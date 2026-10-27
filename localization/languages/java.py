"""java的测试与源码语法规则；不执行被分析代码。"""

from .common import TestTreeRoute, walk

class TestJavaRoute(TestTreeRoute):
    language = 'java'
    extensions = {'.java'}
    namespace_nodes = set()
    type_nodes = {'class_declaration', 'interface_declaration', 'enum_declaration', 'record_declaration'}
    call_nodes = {'method_invocation', 'object_creation_expression'}
    import_nodes = {'import_declaration'}
    external_names = {'java', 'javax', 'System', 'String', 'Integer', 'Math', 'Objects', 'Collections'}

    def __init__(self):
        import tree_sitter_java
        super().__init__(tree_sitter_java.language())
        self.junit5 = False

    def prefix(self, root, source):
        self.junit5 = b'org.junit.jupiter' in source.raw
        package = next((n for n in root.named_children if n.type == 'package_declaration'), None)
        return self.text(source, package).removeprefix('package').strip().rstrip(';') if package else ''

    def classify(self, node, source, container):
        if node.type not in {'method_declaration', 'constructor_declaration'}:
            return
        name = self.text(source, node.child_by_field_name('name'))
        modifiers = next((n for n in node.named_children if n.type == 'modifiers'), None)
        annotations = [self.text(source, n.child_by_field_name('name')) for n in walk(modifiers) if 'annotation' in n.type and n.child_by_field_name('name')] if modifiers else []
        annotated = {a.rsplit('.', 1)[-1] for a in annotations}
        test = bool(annotated & {'Test', 'ParameterizedTest', 'RepeatedTest'})
        fixture = node.type == 'constructor_declaration' or bool(annotated & {'Before', 'After', 'BeforeClass', 'AfterClass', 'BeforeEach', 'AfterEach', 'BeforeAll', 'AfterAll'})
        params = 'dynamic' if annotated & {'ParameterizedTest', 'RepeatedTest'} else 'none'
        return (name, 'test' if test else 'fixture' if fixture else 'helper', params, 'junit5' if self.junit5 else 'junit4', container)

    def call_name(self, node, source):
        if node.type == 'object_creation_expression':
            return (self.text(source, node.child_by_field_name('type')), None, True)
        return (self.text(source, node.child_by_field_name('name')), node.child_by_field_name('object'), False)

    def parse(self, bundle):
        from dataclasses import replace
        from .common import Source
        from ..models import Diagnostic
        parsed = super().parse(bundle)
        cases, diagnostics = [], list(parsed.diagnostics)
        for case in parsed.cases:
            file = next(f for f in bundle.files if f.relative_path == case.file_path)
            source = Source(file)
            node = next(n for n in walk(self.parser.parse(source.raw).root_node)
                        if n.type == 'method_declaration' and n.start_byte == case.location.start_byte)
            modifiers = next((n for n in node.named_children if n.type == 'modifiers'), None)
            needed = list(case.support_ids)
            for annotation in walk(modifiers) if modifiers else ():
                name = annotation.child_by_field_name('name') if 'annotation' in annotation.type else None
                if not name or self.text(source, name).rsplit('.', 1)[-1] != 'MethodSource':
                    continue
                import json
                values = [json.loads(self.text(source, n)) for n in walk(annotation) if n.type == 'string_literal'] or [case.name]
                for value in values:
                    matches = [u for u in parsed.support_units if u.kind == 'helper' and
                               u.location.file_path == case.file_path and u.container == case.container and u.name == value]
                    if len(matches) == 1:
                        needed.append(matches[0].unit_id)
                    else:
                        diagnostics.append(Diagnostic('UNRESOLVED_DATA_PROVIDER', '公有数据提供方法缺失或多义：' + value, case.location))
            cases.append(replace(case, support_ids=tuple(dict.fromkeys(needed))))
        return replace(parsed, cases=tuple(cases), diagnostics=tuple(diagnostics))

from .common import SourceTreeRoute, TYPE_KINDS, walk

class SourceJavaRoute(SourceTreeRoute):
    language = 'java'
    extensions = ('.java',)
    wrappers = set()
    namespace_nodes = set()
    method_nodes = {'method_declaration', 'constructor_declaration'}
    binding_nodes = {'formal_parameter', 'spread_parameter', 'variable_declarator'}
    import_nodes = {'import_declaration'}
    call_nodes = {'method_invocation', 'object_creation_expression'}
    dynamic_nodes = set()
    identifier_nodes = {'identifier', 'type_identifier'}
    type_identifier_nodes = {'type_identifier'}
    ignored_identifier_parents = {'package_declaration', 'scoped_identifier', 'scoped_type_identifier', 'modifiers', 'marker_annotation'}
    self_names = {'this', 'super'}
    known_names = {'String', 'Object', 'Integer', 'Long', 'Double', 'Boolean', 'System', 'Math', 'Exception', 'RuntimeException', 'Override'}

    def __init__(self):
        import tree_sitter_java
        super().__init__(tree_sitter_java.language())
        self.external_roots = {'java', 'javax', 'jdk'} | self.known_names

    def namespace(self, root, source):
        node = next((n for n in root.named_children if n.type == 'package_declaration'), None)
        return self.node_text(source, node).removeprefix('package').strip().rstrip(';') if node else ''

    def declaration(self, node, source, owner):
        types = {'class_declaration': 'class', 'interface_declaration': 'interface', 'enum_declaration': 'enum', 'record_declaration': 'class', 'annotation_type_declaration': 'interface'}
        if node.type in set(types) | self.method_nodes:
            name = self.node_text(source, node.child_by_field_name('name'))
            return (name, types.get(node.type, 'constructor' if node.type == 'constructor_declaration' else 'method'), self.signature(node, source))
        if node.type == 'variable_declarator' and owner and (owner.kind in TYPE_KINDS):
            return (self.node_text(source, node.child_by_field_name('name')), 'declaration', None)

    def import_spec(self, node, source):
        value = self.node_text(source, node).removeprefix('import').strip().removeprefix('static').strip().rstrip(';')
        return (value.rstrip('.*'), value.rsplit('.', 1)[-1] if not value.endswith('.*') else None, 'import')

    def call_name(self, node, source):
        if node.type == 'object_creation_expression':
            return (self.node_text(source, node.child_by_field_name('type')), '')
        receiver = node.child_by_field_name('object')
        if receiver and receiver.type == 'object_creation_expression':
            receiver = receiver.child_by_field_name('type')
        return (self.node_text(source, node.child_by_field_name('name')), self.node_text(source, receiver))

    def is_dynamic_call(self, value):
        return value.endswith(('.forName', '.getMethod', '.getDeclaredMethod', '.invoke'))
