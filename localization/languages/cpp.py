"""cpp的测试与源码语法规则；不执行被分析代码。"""

from .common import TestTreeRoute, walk

class TestCppRoute(TestTreeRoute):
    language = 'cpp'
    extensions = {'.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx', '.c'}
    namespace_nodes = {'namespace_definition'}
    type_nodes = {'class_specifier', 'struct_specifier'}
    call_nodes = {'call_expression', 'new_expression'}
    import_nodes = {'preproc_include'}
    external_names = {'std', 'printf', 'fprintf', 'puts', 'malloc', 'free', 'size_t'}

    def __init__(self):
        import tree_sitter_cpp
        super().__init__(tree_sitter_cpp.language())

    def classify(self, node, source, container):
        if node.type != 'function_definition':
            return
        declarator = node.child_by_field_name('declarator')
        fn = next((n for n in walk(declarator) if n.type == 'function_declarator'), None) if declarator else None
        if not fn:
            return
        name = self.text(source, fn.child_by_field_name('declarator'))
        if name in {'TEST', 'TEST_F', 'TEST_P'}:
            params = fn.child_by_field_name('parameters')
            args = [self.text(source, n) for n in params.named_children]
            if len(args) != 2:
                return
            suite, test = args
            return (test, 'test', 'dynamic' if name == 'TEST_P' else 'none', 'googletest', '.'.join(filter(None, [container, suite])))
        short = name.rsplit('::', 1)[-1]
        owner = container.rsplit('.', 1)[-1]
        fixture = short in {'SetUp', 'TearDown', 'SetUpTestSuite', 'TearDownTestSuite', owner, '~' + owner}
        return (short, 'fixture' if fixture else 'helper', 'none', 'googletest', container)

    def call_name(self, node, source):
        if node.type == 'new_expression':
            return (self.text(source, node.child_by_field_name('type')), None, True)
        fn = node.child_by_field_name('function')
        if fn and fn.type == 'field_expression':
            return (self.text(source, fn.child_by_field_name('field')), fn.child_by_field_name('argument'), False)
        return (self.text(source, fn), None, False)

    def parse(self, bundle):
        from dataclasses import replace
        from .common import Source
        from ..models import SupportUnit, stable_id
        parsed = super().parse(bundle)
        support = list(parsed.support_units)
        for file in bundle.files:
            if not file.relative_path.endswith(tuple(self.extensions)):
                continue
            source = Source(file)
            for node in walk(self.parser.parse(source.raw).root_node):
                if node.type != 'call_expression' or self.text(source, node.child_by_field_name('function')) not in {'INSTANTIATE_TEST_SUITE_P', 'INSTANTIATE_TEST_CASE_P'}:
                    continue
                args = node.child_by_field_name('arguments')
                if not args or len(args.named_children) < 2:
                    continue
                suite = self.text(source, args.named_children[1])
                whole = node.parent if node.parent.type == 'expression_statement' else node
                loc = source.location(whole.start_byte, whole.end_byte)
                support.append(SupportUnit(stable_id(file.relative_path, loc.start_byte, 'parameter_data'), suite + '_parameters', suite, loc, 'parameter_data'))
        cases = [replace(c, support_ids=c.support_ids + tuple(u.unit_id for u in support if u.kind == 'parameter_data' and u.container == c.container.rsplit('.', 1)[-1])) for c in parsed.cases]
        return replace(parsed, cases=tuple(cases), support_units=tuple(support))

from .common import SourceTreeRoute, walk

class SourceCppRoute(SourceTreeRoute):
    language = 'cpp'
    extensions = ('.h', '.hh', '.hpp', '.hxx', '.c', '.cc', '.cpp', '.cxx')
    wrappers = {'template_declaration'}
    namespace_nodes = {'namespace_definition'}
    method_nodes = {'function_definition'}
    binding_nodes = {'parameter_declaration', 'init_declarator'}
    import_nodes = {'preproc_include'}
    call_nodes = {'call_expression', 'new_expression'}
    dynamic_nodes = {'preproc_if', 'preproc_ifdef', 'preproc_function_def'}
    identifier_nodes = {'identifier', 'type_identifier'}
    type_identifier_nodes = {'type_identifier'}
    ignored_identifier_parents = {'qualified_identifier', 'namespace_definition', 'type_parameter_declaration'}
    self_names = {'this'}
    known_names = {'std', 'size_t', 'ptrdiff_t', 'uint32_t', 'uint64_t', 'int32_t', 'int64_t'}

    def __init__(self):
        import tree_sitter_cpp
        super().__init__(tree_sitter_cpp.language())
        self.external_roots = {'std'}
        self.internal_ranges = set()
        self.definition_ranges = set()

    def declaration(self, node, source, owner):
        types = {'class_specifier': 'class', 'struct_specifier': 'struct', 'enum_specifier': 'enum'}
        if node.type in types:
            name = self.node_text(source, node.child_by_field_name('name'))
            return (name, types[node.type], None) if name else None
        if node.type in {'function_definition', 'declaration', 'field_declaration'}:
            declarator = node.child_by_field_name('declarator')
            function = next((n for n in walk(declarator) if n.type == 'function_declarator'), None) if declarator else None
            if function:
                name = self.node_text(source, function.child_by_field_name('declarator'))
                whole = node.parent if node.parent and node.parent.type in self.wrappers else node
                point = (source.file_path, whole.start_byte, whole.end_byte)
                if node.type == 'function_definition':
                    self.definition_ranges.add(point)
                ancestor, anonymous = (node.parent, False)
                while ancestor is not None:
                    if ancestor.type == 'namespace_definition' and ancestor.child_by_field_name('name') is None:
                        anonymous = True
                    ancestor = ancestor.parent
                local_static = not owner and any((n.type == 'storage_class_specifier' and self.node_text(source, n) == 'static' for n in node.named_children))
                if local_static or anonymous:
                    self.internal_ranges.add(point)
                return (name, 'method' if owner or '::' in name else 'function', self.signature(function, source))
            if declarator and (not owner):
                name = next((n for n in walk(declarator) if n.type == 'identifier'), None)
                if name:
                    return (self.node_text(source, name), 'declaration', None)
        if node.type in {'preproc_def', 'alias_declaration', 'type_definition'}:
            name = node.child_by_field_name('name') or node.child_by_field_name('declarator')
            if name:
                return (self.node_text(source, name), 'declaration', None)

    def import_spec(self, node, source):
        value = self.node_text(source, node.child_by_field_name('path'))
        return (value.strip('"<>'), None, 'include')

    def signature(self, node, source):
        value = super().signature(node, source)
        qualifiers = [self.node_text(source, child) for child in node.named_children if child.type in {'type_qualifier', 'ref_qualifier'}]
        return value + (' ' + ' '.join(qualifiers) if qualifiers else '') if value is not None else None

    def call_name(self, node, source):
        if node.type == 'new_expression':
            return (self.node_text(source, node.child_by_field_name('type')), '')
        function = node.child_by_field_name('function')
        if function and function.type == 'field_expression':
            return (self.node_text(source, function.child_by_field_name('field')), self.node_text(source, function.child_by_field_name('argument')))
        return (self.node_text(source, function), '')
