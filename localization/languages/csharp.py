"""csharp的测试与源码语法规则；不执行被分析代码。"""

from .common import TestTreeRoute, walk

class TestCSharpRoute(TestTreeRoute):
    language = 'csharp'
    extensions = {'.cs'}
    namespace_nodes = {'namespace_declaration'}
    type_nodes = {'class_declaration', 'struct_declaration', 'record_declaration'}
    call_nodes = {'invocation_expression', 'object_creation_expression'}
    import_nodes = {'using_directive'}
    external_names = {'System', 'Console', 'Math', 'String', 'string', 'int', 'Guid', 'Task'}

    def __init__(self):
        import tree_sitter_c_sharp
        super().__init__(tree_sitter_c_sharp.language())

    def prefix(self, root, source):
        ns = next((n for n in root.named_children if n.type == 'file_scoped_namespace_declaration'), None)
        return self.text(source, ns.child_by_field_name('name')) if ns else ''

    def classify(self, node, source, container):
        if node.type not in {'method_declaration', 'constructor_declaration'}:
            return
        name = self.text(source, node.child_by_field_name('name'))
        attrs = {self.text(source, n.child_by_field_name('name')).rsplit('.', 1)[-1] for group in node.named_children if group.type == 'attribute_list' for n in walk(group) if n.type == 'attribute'}
        test = bool(attrs & {'Fact', 'Theory', 'Test', 'TestCase', 'TestMethod', 'DataTestMethod'})
        fixture = node.type == 'constructor_declaration' or name == 'Dispose' or bool(attrs & {'SetUp', 'TearDown', 'OneTimeSetUp', 'OneTimeTearDown', 'TestInitialize', 'TestCleanup', 'ClassInitialize', 'ClassCleanup'})
        framework = 'xunit' if attrs & {'Fact', 'Theory'} else 'nunit' if attrs & {'Test', 'TestCase'} else 'mstest'
        params = 'static' if attrs & {'InlineData', 'TestCase', 'DataRow'} else 'dynamic' if attrs & {'Theory', 'DataTestMethod'} else 'none'
        return (name, 'test' if test else 'fixture' if fixture else 'helper', params, framework, container)

    def call_name(self, node, source):
        if node.type == 'object_creation_expression':
            return (self.text(source, node.child_by_field_name('type')), None, True)
        fn = node.child_by_field_name('function')
        if fn and fn.type == 'member_access_expression':
            return (self.text(source, fn.child_by_field_name('name')), fn.child_by_field_name('expression'), False)
        return (self.text(source, fn), None, False)

from .common import SourceTreeRoute, walk

class SourceCSharpRoute(SourceTreeRoute):
    language = 'csharp'
    extensions = ('.cs',)
    wrappers = set()
    namespace_nodes = {'namespace_declaration'}
    method_nodes = {'method_declaration', 'constructor_declaration', 'operator_declaration', 'conversion_operator_declaration'}
    binding_nodes = {'parameter', 'variable_declarator'}
    import_nodes = {'using_directive'}
    call_nodes = {'invocation_expression', 'object_creation_expression'}
    dynamic_nodes = {'preproc_if', 'preproc_elif'}
    identifier_nodes = {'identifier', 'generic_name'}
    type_identifier_nodes = set()
    ignored_identifier_parents = {'file_scoped_namespace_declaration', 'qualified_name', 'modifier', 'attribute'}
    self_names = {'this', 'base'}
    known_names = {'System', 'String', 'Object', 'Int32', 'Int64', 'Double', 'Boolean', 'Console', 'Math', 'Exception'}

    def __init__(self):
        import tree_sitter_c_sharp
        super().__init__(tree_sitter_c_sharp.language())
        self.external_roots = self.known_names | {'System'}

    def namespace(self, root, source):
        node = next((n for n in root.named_children if n.type == 'file_scoped_namespace_declaration'), None)
        return self.node_text(source, node.child_by_field_name('name')) if node else ''

    def is_partial(self, node, source):
        return any((n.type == 'modifier' and self.node_text(source, n) == 'partial' for n in node.named_children))

    def declaration(self, node, source, owner):
        types = {'class_declaration': 'class', 'struct_declaration': 'struct', 'interface_declaration': 'interface', 'enum_declaration': 'enum', 'record_declaration': 'class'}
        if node.type in types or node.type in self.method_nodes | {'property_declaration'}:
            name = self.node_text(source, node.child_by_field_name('name')) or node.type
            params = node.child_by_field_name('type_parameters') or next((n for n in node.named_children if n.type == 'type_parameter_list'), None)
            if node.type in types and params:
                name += '`' + str(len(params.named_children))
            kind = types.get(node.type, 'constructor' if node.type == 'constructor_declaration' else 'declaration' if node.type == 'property_declaration' else 'method')
            return (name, kind, self.signature(node, source))
        if node.type == 'variable_declarator' and owner and (owner.kind in {'class', 'struct'}):
            return (self.node_text(source, node.child_by_field_name('name')), 'declaration', None)

    def import_spec(self, node, source):
        value = self.node_text(source, node).removeprefix('using').strip().removeprefix('static').strip().rstrip(';')
        if '=' in value:
            alias, value = (v.strip() for v in value.split('=', 1))
            return (value, alias, 'import')
        return (value, None, 'import')

    def call_name(self, node, source):
        if node.type == 'object_creation_expression':
            return (self.node_text(source, node.child_by_field_name('type')), '')
        function = node.child_by_field_name('function')
        if function and function.type == 'member_access_expression':
            return (self.node_text(source, function.child_by_field_name('name')), self.node_text(source, function.child_by_field_name('expression')))
        return (self.node_text(source, function), '')

    def is_dynamic_call(self, value):
        return value.endswith(('.GetType', '.GetMethod', '.Invoke', '.CreateInstance'))

    def _resolve(self, ref, inventory):
        from dataclasses import replace
        import re
        value = ref.spelling
        if '<' in value:
            # 将泛型类型引用与声明的元数名称关联，避免落到同名非泛型接口。
            def arity(match):
                return match[1] + '`' + str(match[2].count(',') + 1)
            value = re.sub(r'([\w.]+)<([^<>]+)>', arity, value)
            ref = replace(ref, spelling=value)
        return super()._resolve(ref, inventory)
