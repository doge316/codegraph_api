"""rust的测试与源码语法规则；不执行被分析代码。"""

from .common import TestTreeRoute, walk

class TestRustRoute(TestTreeRoute):
    language = 'rust'
    extensions = {'.rs'}
    namespace_nodes = {'mod_item'}
    type_nodes = {'struct_item', 'enum_item', 'trait_item'}
    call_nodes = {'call_expression'}
    import_nodes = {'use_declaration'}
    external_names = {'std', 'core', 'alloc', 'String', 'Vec', 'Some', 'Ok', 'Err', 'Box', 'Rc', 'Arc', 'HashMap', 'HashSet', 'RefCell', 'Duration'}

    def __init__(self):
        import tree_sitter_rust
        super().__init__(tree_sitter_rust.language())

    def classify(self, node, source, container):
        if node.type != 'function_item':
            return
        name = self.text(source, node.child_by_field_name('name'))
        attrs = []
        previous = node.prev_named_sibling
        while previous and previous.type in {'attribute_item', 'line_comment', 'block_comment'}:
            if previous.type == 'attribute_item':
                attrs.extend((self.text(source, n) for n in walk(previous) if n.type == 'identifier'))
            previous = previous.prev_named_sibling
        return (name, 'test' if 'test' in attrs else 'helper', 'none', 'libtest', container)

    def call_name(self, node, source):
        fn = node.child_by_field_name('function')
        if fn and fn.type == 'field_expression':
            return (self.text(source, fn.child_by_field_name('field')), fn.child_by_field_name('value'), False)
        name = self.text(source, fn)
        return (name, None, name.endswith(('::new', '::default')))
