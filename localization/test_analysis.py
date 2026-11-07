"""公测快照、基础图查询与测试语法补充；用例直接以文件、名称和起始行关联。"""
import hashlib
import re
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile

from .codegraph import CodeGraph, GraphError
from .config import files_under, public_directories, SDK_LANGUAGES
from .languages.common import SourceText, checked_content, location, diagnostic, key_id
from .models import PublicBundle, PublicFile, ParsedTests, TestCase, SupportUnit

EXTENSIONS = {'python': ('.py', '.pyi'), 'java': ('.java',), 'csharp': ('.cs',),
              'cpp': ('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx'), 'rust': ('.rs',)}


def reference_identity(ref):
    if not isinstance(ref, dict) or set(ref) != {'filePath', 'name', 'startLine'}:
        raise ValueError('用例必须包含filePath、name、startLine')
    file, name, line = ref['filePath'], ref['name'], ref['startLine']
    if not isinstance(file, str) or not file or not isinstance(name, str) or not name.strip():
        raise ValueError('用例路径及名称不能为空')
    path = Path(file)
    if path.is_absolute() or '..' in path.parts or '\\' in file or ':' in file or path.as_posix() != file:
        raise ValueError('filePath须为规范的工程内相对路径')
    if type(line) is not int or line < 1:
        raise ValueError('startLine须为正整数')
    return file, name, line


def case_reference(case):
    return dict(zip(('filePath', 'name', 'startLine'), case.case_id))


def public_bundle(root, roots, extensions):
    root = Path(root).resolve(strict=True)
    roots = [Path(p).resolve(strict=True) for p in roots]
    if not roots or any(not p.is_dir() or not p.is_relative_to(root) for p in roots):
        raise ValueError('公测目录不存在或越界')
    files = {}
    for public in roots:
        for path in files_under(public):
            raw = path.read_bytes()
            role = 'test' if path.suffix in extensions else 'resource'
            if path.suffix == '.py' and not (path.name.startswith('test') or path.name.endswith('_test.py')):
                role = 'helper'
            name = path.relative_to(root).as_posix()
            files[name] = PublicFile(name, raw, hashlib.sha256(raw).hexdigest(), role)
    values = tuple(files[k] for k in sorted(files))
    digest = hashlib.sha256(repr([(f.relative_path, f.sha256) for f in values]).encode()).hexdigest()
    return PublicBundle(root, values, digest)


def read_public(service, repo):
    root = (repo / 'target_project').resolve()
    if not root.is_relative_to(repo.resolve()) or not root.is_dir():
        raise ValueError('target_project不存在或越界')
    roots = public_directories(root)
    parser = UpstreamTests(SDK_LANGUAGES[service.target], service.graph)
    return public_bundle(root, roots, parser.extensions), roots, parser


def unchanged(bundle, roots=None):
    check_snapshot(source_snapshot(bundle), roots=roots)


def source_snapshot(bundle):
    return {'root': str(bundle.root), 'files': {f.relative_path: f.sha256 for f in bundle.files}}


def check_snapshot(snapshot, *, roots=None):
    root = Path(snapshot['root'])
    roots = roots or public_directories(root)
    current = {p.relative_to(root).as_posix(): p for public in roots for p in files_under(public)}
    if set(current) != set(snapshot['files']):
        raise ValueError('SOURCE_CHANGED：公测文件清单变化')
    for name, digest in snapshot['files'].items():
        if hashlib.sha256(current[name].read_bytes()).hexdigest() != digest:
            raise ValueError('SOURCE_CHANGED：公测文件变化：' + name)


def read_review_files(file_paths, snapshot):
    """模型复核前读取同一快照的完整文件，不允许路径替换或文件改动。"""
    check_snapshot(snapshot)
    root = Path(snapshot['root']).resolve()
    result, seen = [], set()
    for value in file_paths:
        path = Path(value)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('复核文件路径越界或被替换为链接')
        name = path.resolve().relative_to(root).as_posix()
        if name not in snapshot['files']:
            raise ValueError('复核文件不属于静态分析快照')
        if name in seen:
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot['files'][name]:
            raise ValueError('SOURCE_CHANGED：复核文件变化：' + name)
        seen.add(name)
        result.append({'filePath': name, 'startLine': 1,
                       'source': SourceText(name, raw, name.endswith(('.py', '.pyi'))).text})
    return result


def without_future_tests(file, future, language):
    raw = bytearray(file.content_bytes)
    for case in future:
        loc = case.location
        raw[loc.start_byte:loc.end_byte] = bytes(b if b in (10, 13) else 32 for b in raw[loc.start_byte:loc.end_byte])
        if language == 'Python':
            if loc.end_byte - loc.start_byte < 4:
                raise ValueError('无法放入Python占位语句')
            raw[loc.start_byte:loc.start_byte + 4] = b'pass'
    return bytes(raw)


class UpstreamTests:
    """基础图查询负责节点、归属和直接调用；本地补充框架语义与精确范围。"""

    def __init__(self, language, graph=None):
        self.language, self.extensions = language, EXTENSIONS[language]
        self.graph = graph or CodeGraph({})

    @contextmanager
    def _roots(self, bundle):
        actual = all((bundle.root / f.relative_path).is_file() and
                     (bundle.root / f.relative_path).read_bytes() == f.content_bytes for f in bundle.files)
        if actual:
            yield [(p, p.relative_to(bundle.root).as_posix()) for p in
                   (getattr(self, 'public_roots', None) or public_directories(bundle.root))]
        else:
            with tempfile.TemporaryDirectory(prefix='scopetrans-public-') as temporary:
                root = Path(temporary)
                for file in bundle.files:
                    path = root / file.relative_path
                    if not path.resolve().is_relative_to(root):
                        raise ValueError('公测快照路径越界')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(file.content_bytes)
                yield [(root, '')]

    def parse(self, bundle):
        self.calls, self.review_cases = {}, set()
        self.context_references, self.context_opaque = set(), False
        sources = {f.relative_path: SourceText(f.relative_path, f.content_bytes, self.language == 'python')
                   for f in bundle.files if f.relative_path.endswith(self.extensions)}
        native, graph_calls, diagnostics = {}, {}, []
        with self._roots(bundle) as roots:
            for root, prefix in roots:
                prefix = '' if prefix == '.' else prefix
                self.graph.prepare(root, self.language)
                nodes = self.graph.functions(root)
                for file, source in sources.items():
                    path = Path(file)
                    if prefix:
                        if not path.is_relative_to(prefix):
                            continue
                        relative = path.relative_to(prefix).as_posix()
                    else:
                        relative = file
                    returned = self.graph.content(root, file=relative)
                    if returned['filePath'] != relative or returned['source'].lstrip('\ufeff') != source.text.lstrip('\ufeff'):
                        raise ValueError('SOURCE_CHANGED：上游公测原文与快照不一致：' + file)
                for node in nodes:
                    file = (Path(prefix) / node['filePath']).as_posix()
                    if file not in sources:
                        raise ValueError('上游函数清单包含公测快照之外的文件：' + file)
                    key = (file, node['name'], node['startLine'])
                    native[key] = node
                    try:
                        located = self.graph.location(root, node)
                        if located['filePath'] != node['filePath']:
                            raise ValueError('SOURCE_CHANGED：上游公测节点位置不一致')
                        container = located.get('container')
                        if container:
                            native[key] = {**node, 'container': {**container,
                                'filePath': (Path(prefix) / container['filePath']).as_posix()}}
                        selector = located.get('container') or node
                        content = self.graph.content(root, node=selector)
                        source = sources.get((Path(prefix) / content['filePath']).as_posix())
                        if source is None or content['source'].lstrip('\ufeff') not in source.text.lstrip('\ufeff'):
                            raise ValueError('SOURCE_CHANGED：上游公测节点原文不一致')
                        graph_calls[key] = [dict(row, filePath=(Path(prefix) / row['filePath']).as_posix())
                                            for row in self.graph.callees(root, node).get('functions', ())]
                    except GraphError as exc:
                        if exc.code != 'AMBIGUOUS_NODE':
                            raise
                        diagnostics.append(diagnostic('GRAPH_SYMBOL_UNRESOLVED', '公测key存在歧义，补充核对完整声明：' + node['name'], blocking=False))
        context = _TestSyntax(self.language, sources, native, graph_calls)
        parsed = context.parse()
        self.calls, self.review_cases = context.calls, context.review
        self.context_references, self.context_opaque = context.context_references, context.context_opaque
        self.parsed = replace(parsed, diagnostics=tuple(diagnostics) + parsed.diagnostics)
        return self.parsed


_TEST_TYPES = {'class_definition', 'class_declaration', 'record_declaration', 'struct_declaration',
               'interface_declaration', 'class_specifier', 'struct_specifier', 'impl_item'}
_TEST_FUNCTIONS = {'function_definition', 'method_declaration', 'constructor_declaration', 'function_item'}
_TEST_CALLS = {'call', 'call_expression', 'method_invocation', 'object_creation_expression', 'invocation_expression'}


def _walk(node):
    if node is not None:
        yield node
        for child in node.named_children:
            yield from _walk(child)


def _field(node, name):
    return node.child_by_field_name(name) if node is not None else None


def _text(node):
    return node.text.decode('utf-8') if node is not None else ''


def _declarator(node):
    node = _field(node, 'declarator')
    while node is not None and node.type not in {'identifier', 'field_identifier', 'qualified_identifier'}:
        node = _field(node, 'declarator') or next((c for c in node.named_children if c.type != 'parameter_list'), None)
    return node


def _parameters(node):
    params = _field(node, 'parameters')
    if params is None:
        decl = next((n for n in _walk(_field(node, 'declarator')) if n.type == 'function_declarator'), None)
        params = _field(decl, 'parameters')
    result = []
    for item in params.named_children if params is not None else ():
        binding = _field(item, 'name') or _field(item, 'pattern') or _declarator(item)
        if binding is None:
            binding = item if item.type == 'identifier' else next((c for c in item.named_children if c.type == 'identifier'), None)
        name = _text(binding)
        if name and name not in {'self', 'cls'}:
            result.append((name, _text(_field(item, 'type'))))
    return result


def _framework(name):
    return bool(re.match(r'^(?:pytest(?:\.|$)|unittest(?:\.|$)|builtins\.|java\.|javax\.|System\.|std\.|testing\.|org\.junit\.|Assert\.|Assertions\.|assert|EXPECT_|ASSERT_|(?:self|cls)\.assert)', name) or
                name in {'len', 'str', 'int', 'float', 'bool', 'list', 'dict', 'tuple', 'set', 'range', 'print',
                         'isinstance', 'issubclass', 'sum', 'min', 'max', 'abs', 'enumerate', 'zip', 'sorted',
                         'Exception', 'ValueError', 'TypeError', 'RuntimeError', 'ArithmeticException', 'fail', 'GetParam'})


class _TestSyntax:
    """补充SDK未提供的测试框架、局部传值和初始化信息；不执行测试源码。"""

    def __init__(self, language, sources, native, graph_calls):
        import importlib
        from tree_sitter import Language, Parser
        grammar = importlib.import_module('tree_sitter_' + {'csharp': 'c_sharp'}.get(language, language))
        self.syntax = Parser(Language(grammar.language()))
        self.language, self.sources, self.native, self.graph_calls = language, sources, native, graph_calls
        self.encoded = {file: source.text.encode('utf-8') for file, source in sources.items()}
        self.files, self.symbols, self.diagnostics = {}, [], []
        syntax_errors = set()
        self.calls, self.review = {}, set()
        self.context_references, self.context_opaque = set(), False
        for file, source in sources.items():
            tree = self.syntax.parse(self.encoded[file])
            if tree.root_node.has_error:
                syntax_errors.add(file)
            self.files[file] = tree
            self._collect(file, tree.root_node)
        for symbol in self.symbols:
            container = symbol.native.get('container') if symbol.native else None
            if not container or symbol.owner is not None and symbol.owner.kind == 'function':
                continue
            matches = [s for s in self.symbols if s.kind == 'type' and s.file == container['filePath'] and
                       s.name == container['name'] and s.location.start_line <= container['startLine'] <= s.ast.start_point.row + 1]
            if len(matches) == 1:
                symbol.owner = matches[0]
        self.tests = [s for s in self.symbols if self._is_test(s)]
        for file in sorted(syntax_errors):
            tests = [s for s in self.tests if s.file == file]
            if self.language == 'cpp' and self._complete_cpp_tests(file, tests):
                # 部分合法C++声明超出当前语法库能力时，不裁剪错误附近的代码
                # 完整宏头和闭合函数体须与独立词法清单一致，随后保守合为单批
                self.context_opaque = True
                self.diagnostics.append(diagnostic('PARTIAL_CPP_PARSE',
                    'C++局部语法无法完整解析，保留完整公测原文并合批：' + file, blocking=False))
            else:
                self.diagnostics.append(diagnostic('PARSE_ERROR', '公测语法树存在错误：' + file))
        for test in self.tests:
            test.case_id = (test.file, test.native['name'] if test.native else test.name,
                            test.native['startLine'] if test.native else test.ast.start_point.row + 1)
            if test.file in syntax_errors:
                self.review.add(test.case_id)
        self.known_types = {s.name for s in self.symbols if s.kind == 'type'}
        self.known_types.update(_text(n) for tree in self.files.values() for n in _walk(tree.root_node) if n.type == 'type_identifier')

    def _complete_cpp_tests(self, file, tests):
        """仅允许已完整识别的GoogleTest声明走不裁剪的保守分支。"""
        source = self.sources[file].text
        def blank(match):
            return ''.join('\n' if c == '\n' else ' ' for c in match.group())
        masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'', blank, source)
        expected = {(m.group(1), masked[:m.start()].count('\n') + 1) for m in re.finditer(
            r'\b(TEST(?:_F|_P)?)\s*\(\s*[A-Za-z_]\w*\s*,\s*[A-Za-z_]\w*\s*\)\s*\{', masked)}
        actual = {(s.name, s.ast.start_point.row + 1) for s in tests}
        if not expected or actual != expected:
            return False
        for test in tests:
            body = _field(test.ast, 'body')
            if body is None or not body.children or body.children[-1].type != '}' or body.children[-1].is_missing:
                return False
        return True

    def _location(self, file, node=None, *, start=None, end=None):
        source = self.sources[file]
        encoded = self.encoded[file]
        start = node.start_byte if start is None else start
        end = node.end_byte if end is None else end
        # Python解码移除了BOM；其他语言的BOM仍占用语法树输入的三个字节
        bom = 3 if source.encoding == 'utf-8-sig' else 0
        first = len(encoded[:start].decode('utf-8').encode(source.codec)) + bom
        last = len(encoded[:end].decode('utf-8').encode(source.codec)) + bom
        return source.location(first, last)

    def _collect(self, file, root):
        def visit(node, owner=None):
            is_type, is_function = node.type in _TEST_TYPES, node.type in _TEST_FUNCTIONS
            next_owner = owner
            if is_type or is_function:
                name = _text(_field(node, 'name')) or _text(_declarator(node))
                if name:
                    span = node.parent if node.parent is not None and node.parent.type in {'decorated_definition', 'template_declaration'} else node
                    start = span.start_byte
                    if self.language == 'rust':
                        previous = node.prev_named_sibling
                        while previous is not None and previous.type in {'attribute_item', 'line_comment', 'block_comment'}:
                            start = previous.start_byte
                            previous = previous.prev_named_sibling
                    loc = self._location(file, start=start, end=span.end_byte)
                    native = [n for (f, base, line), n in self.native.items() if f == file and base == name and
                              loc.start_line <= line <= node.start_point.row + 1]
                    tags = self.sources[file].raw[loc.start_byte:self._location(file, node).start_byte].decode(self.sources[file].codec)
                    tags += '\n' + '\n'.join(_text(n) for n in node.named_children if n.type in {'modifiers', 'attribute_list'})
                    symbol = SimpleNamespace(file=file, ast=node, owner=owner, kind='type' if is_type else 'function',
                        name=name, qualified=(owner.qualified + '.' if owner else '') + name, location=loc,
                        syntax_start=start, syntax_end=span.end_byte,
                        tags=tags, native=native[0] if len(native) == 1 else None)
                    self.symbols.append(symbol)
                    next_owner = symbol
            for child in node.named_children:
                visit(child, next_owner)
        visit(root)

    @staticmethod
    def _fixture(symbol):
        return bool(re.search(r'(?:\.|@)(?:yield_)?fixture\b', symbol.tags))

    @staticmethod
    def _alias(symbol):
        match = re.search(r'\bname\s*=\s*[\'"]([^\'"]+)', symbol.tags)
        return match.group(1) if match else symbol.name

    @staticmethod
    def _lifecycle(symbol):
        return bool(re.search(r'\b(?:BeforeEach|BeforeAll|AfterEach|AfterAll|Before|After|SetUp|TearDown|OneTimeSetUp|OneTimeTearDown)\b', symbol.tags) or
                    symbol.name in {'setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'setup_method', 'teardown_method', 'setup_class', 'teardown_class', 'setup_module', 'teardown_module', 'SetUp', 'TearDown'})

    def _is_test(self, s):
        if s.kind != 'function' or s.owner is not None and s.owner.kind == 'function' or self._fixture(s):
            return False
        if self.language == 'python':
            filename = Path(s.file).name
            if not s.name.startswith('test') or not re.fullmatch(r'(?:test_.*|.*_test)\.py', filename):
                return False
            scopes = [self.files[s.file].root_node]
            owner = s.owner
            while owner is not None:
                unittest = 'TestCase' in _text(_field(owner.ast, 'superclasses'))
                if not owner.name.startswith('Test') and not unittest:
                    return False
                scopes.append(_field(owner.ast, 'body'))
                owner = owner.owner
            for scope in scopes:
                enabled = True
                for statement in scope.named_children if scope is not None else ():
                    value = statement
                    if statement.type == 'expression_statement' and statement.named_children:
                        value = statement.named_children[0]
                    if value.type == 'assignment' and _text(_field(value, 'left')) == '__test__':
                        enabled = _text(_field(value, 'right')) not in {'False', '0', 'None'}
                if not enabled:
                    return False
            return True
        if self.language == 'java':
            return bool(re.search(r'@(?:[\w.]+\.)?(?:Test|ParameterizedTest|RepeatedTest)\b', s.tags))
        if self.language == 'csharp':
            return bool(re.search(r'\[(?:[\w.]+\.)?(?:Fact|Theory|Test|TestCase|TestCaseSource|TestMethod|DataTestMethod)\b', s.tags))
        if self.language == 'rust':
            return bool(re.search(r'#\s*\[\s*(?:[\w:]+::)?test\s*\]', s.tags))
        return self.language == 'cpp' and s.name in {'TEST', 'TEST_F', 'TEST_P'}

    def _owners(self, test, reasons):
        owner = test.owner
        if owner is None and self.language == 'cpp' and test.name in {'TEST_F', 'TEST_P'}:
            decl = next((n for n in _walk(_field(test.ast, 'declarator')) if n.type == 'function_declarator'), None)
            params = _field(decl, 'parameters')
            name = _text(_field(params.named_children[0], 'type')) if params is not None and params.named_children else ''
            matches = [s for s in self.symbols if s.kind == 'type' and s.name == name]
            if len(matches) == 1:
                owner = matches[0]
            else:
                reasons.add('GoogleTest fixture类型未确认：' + name)
        result, seen = [], set()
        def visit(current):
            if current is None or id(current) in seen:
                return
            seen.add(id(current)); result.append(current)
            bases = _field(current.ast, 'superclasses') or _field(current.ast, 'superclass') or next(
                (n for n in current.ast.named_children if n.type in {'base_list', 'base_class_clause'}), None)
            for name in {_text(n) for n in _walk(bases) if n.type in {'identifier', 'type_identifier'}}:
                matches = [s for s in self.symbols if s.kind == 'type' and s.name == name and s is not current]
                if len(matches) == 1:
                    visit(matches[0])
                elif len(matches) > 1:
                    reasons.add('测试基类存在歧义：' + name)
        visit(owner)
        return result

    def _fixture_for(self, name, test, owners, reasons):
        directory = Path(test.file).parent
        matches = []
        for s in self.symbols:
            if s.kind != 'function' or not self._fixture(s) or self._alias(s) != name:
                continue
            rank = -1
            if s.owner is not None and any(s.owner is o for o in owners):
                rank = 10000 - next(i for i,o in enumerate(owners) if o is s.owner)
            elif s.file == test.file and s.owner is None:
                rank = 9000
            elif s.owner is None and Path(s.file).name == 'conftest.py' and directory.is_relative_to(Path(s.file).parent):
                rank = len(Path(s.file).parent.parts)
            if rank >= 0:
                matches.append((rank, s))
        matches.sort(key=lambda row: -row[0])
        if len(matches)>1 and matches[0][0]==matches[1][0]:
            reasons.add('fixture存在歧义：' + name)
            return None
        return matches[0][1] if matches else None

    def _helper_for(self, name, owner, owners):
        short = name.rsplit('.',1)[-1]
        prefix = name.rpartition('.')[0]
        if owner.native:
            # 首选已解析的直接调用目标；按实际调用名称和接收者范围核对，防止同名误连
            key = (owner.file, owner.native['name'], owner.native['startLine'])
            targets = self.graph_calls.get(key, ())
            direct = [s for s in self.symbols if s.kind == 'function' and s.name == short and
                      any(t['filePath'] == s.file and t['name'] == s.name and
                          s.location.start_line <= t['startLine'] <= s.location.end_line for t in targets) and
                      (not prefix or prefix in {'self','this','cls',s.owner.name if s.owner else '',Path(s.file).stem})]
            if len(direct) == 1:
                return direct[0]
        candidates = [s for s in self.symbols if s.kind=='function' and s.name==short and
            (s.file == owner.file or s.owner is not None and any(s.owner is o for o in owners)) and
            (not prefix or prefix in {'self','this','cls', owner.owner.name if owner.owner else ''})]
        if len(candidates)>1 and owners:
            for cls in owners:
                nearest=[s for s in candidates if s.owner is cls]
                if nearest:
                    candidates=nearest
                    break
        if len(candidates)!=1:
            imports = '\n'.join(_text(n) for n in self.files[owner.file].root_node.named_children if n.type.startswith('import'))
            candidates=[s for s in self.symbols if s.kind=='function' and s.name==short and s.file!=owner.file and
                        (prefix and (s.qualified.endswith(name) or Path(s.file).stem==prefix) or
                         not prefix and re.search(r'\b'+re.escape(short)+r'\b',imports) and Path(s.file).stem in imports)]
        if len(candidates)>1 and owner.native:
            key=(owner.file,owner.native['name'],owner.native['startLine'])
            targets=self.graph_calls.get(key,())
            candidates=[s for s in candidates if any(t['filePath']==s.file and t['name']==s.name and
                        s.location.start_line<=t['startLine']<=s.location.end_line for t in targets)]
        return candidates[0] if len(candidates)==1 else None

    def parse(self):
        cases, support = [], {}
        for test in self.tests:
            reasons, selected, interfaces, facts = set(), {}, set(), []
            owners = self._owners(test, reasons)
            active, fixture_stack, fixture_values = set(), set(), {}
            add_support = lambda s: selected.setdefault(id(s),s)

            def combine(values):
                values=list(values)
                value=next((v for v in reversed(values) if v.get('producer')), {})
                return {**value,'callIds':list(dict.fromkeys(c for v in values for c in v.get('callIds',()))),
                        'origins':list(dict.fromkeys(c for v in values for c in v.get('origins',([v['producer']] if v.get('producer') else []))))}

            def analyze(owner,incoming=None,exported=None,body=None):
                if id(owner) in active:
                    return {}
                active.add(id(owner))
                scope={}
                for cls in owners:
                    for node in _walk(_field(cls.ast,'body')):
                        if node.type!='field_declaration':continue
                        declaration=next((n for n in node.named_children if n.type=='variable_declaration'),node)
                        typename=_text(_field(declaration,'type'))
                        for binding in [_declarator(node), *[ _field(n,'name') for n in declaration.named_children if n.type=='variable_declarator']]:
                            if binding is not None and typename:
                                scope[_text(binding)]={'hint':typename,'producer':typename,'variable':_text(binding)}
                for node in _walk(self.files[owner.file].root_node):
                    if node.type not in {'import_statement','import_from_statement'}:continue
                    parent=node.parent
                    while parent is not None and parent.type not in _TEST_FUNCTIONS:
                        parent=parent.parent
                    if parent is not None and parent!=owner.ast:continue
                    module=_field(node,'module_name')
                    for child in node.named_children:
                        if child==module:continue
                        name=_text(_field(child,'name')) or _text(child)
                        alias=_text(_field(child,'alias')) or (name.rsplit('.',1)[-1] if module is not None else name.split('.')[0])
                        scope[alias]={'hint':name.rsplit('.',1)[-1],'importName':name.rsplit('.',1)[-1],
                                      'importPath':'.'.join(filter(None,[_text(module),name]))}
                scope.update(incoming or {})
                returned=[]

                def asserted(node,values,offset=0):
                    value=combine(values)
                    for fact in facts:
                        if fact.get('callId') in value['callIds']:fact['asserted']=True
                    return value

                def expr(node,offset=0):
                    if node is None:return {}
                    text=_text(node)
                    if text in scope:return scope[text]
                    if node.type in {'string','string_literal'}:return {'hint':'str' if self.language=='python' else 'String'}
                    if node.type in {'integer','integer_literal','number_literal','float','true','false'}:return {}
                    if node.type in {'identifier','field_identifier','self'}:return scope.get(text,{})
                    if node.type in _TEST_FUNCTIONS | {'lambda','lambda_expression'}:
                        saved=dict(scope)
                        for name,typename in _parameters(node):scope[name]={'hint':typename,'variable':name}
                        value=expr(_field(node,'body'),offset);scope.clear();scope.update(saved)
                        return value
                    if node.type in {'assignment','assignment_expression','variable_declarator','init_declarator','let_declaration','assignment_statement','annotated_assignment'}:
                        target=_field(node,'left') or _field(node,'name') or _field(node,'pattern') or _field(node,'declarator')
                        rhs=_field(node,'right') or _field(node,'value') or next((c for c in node.named_children if c!=target and c.type in _TEST_CALLS),None)
                        value=expr(rhs,offset)
                        typename=_text(_field(node,'type') or _field(node.parent,'type'))
                        if target is not None:
                            targets=[n for n in _walk(target) if n.type=='identifier'] if target.type in {'tuple_pattern','tuple','pattern_list','structured_binding_declarator'} else [target]
                            for binding in targets:
                                scope[_text(binding)]={**value,'variable':_text(binding),**({'hint':typename} if typename and typename not in {'var','auto'} else {})}
                        return value
                    if node.type=='declaration' and self.language=='cpp':
                        target=_field(node,'declarator');typename=_text(_field(node,'type'))
                        if target is not None and target.type=='identifier':
                            value={'hint':typename,'producer':typename,'variable':_text(target)};scope[_text(target)]=value
                            if not _framework(typename):interfaces.add(typename)
                            return value
                    if node.type=='macro_invocation' and self.language=='rust':
                        tokens=next((c for c in node.named_children if c.type=='token_tree'),None)
                        if tokens is None:return {}
                        prefix=b'fn __probe(){ __check('
                        probe=self.syntax.parse(prefix+tokens.text[1:-1]+b'); }')
                        call=next((n for n in _walk(probe.root_node) if n.type=='call_expression'),None)
                        args=_field(call,'arguments')
                        if args is None:
                            reasons.add('Rust宏内容无法完整解析');return {}
                        shift=tokens.start_byte+1+offset-len(prefix)
                        return asserted(node,[expr(n,shift) for n in args.named_children],offset)
                    if node.type in _TEST_CALLS:
                        construct=node.type=='object_creation_expression'
                        callable=_field(node,'function')
                        name=_text(_field(node,'type')) if construct else _text(_field(node,'name') or callable)
                        receiver=_field(node,'object')
                        if callable is not None and callable.type in {'attribute','field_expression','member_access_expression'}:
                            receiver=_field(callable,'object') or _field(callable,'argument') or _field(callable,'expression') or _field(callable,'value')
                            name=_text(_field(callable,'attribute') or _field(callable,'field') or _field(callable,'name'))
                        name=name.replace('::','.').replace('->','.')
                        value=expr(receiver,offset) if receiver is not None else {}
                        prefix=value.get('hint') or _text(receiver)
                        if receiver is not None:name=prefix+'.'+name
                        elif scope.get(name,{}).get('importName'):name=scope[name]['importName']
                        args=_field(node,'arguments')
                        values=[expr(n.named_children[-1] if n.type=='argument' else _field(n,'value') or n,offset) for n in args.named_children] if args is not None else []
                        helper=self._helper_for(name,owner,owners)
                        if helper is not None:
                            add_support(helper)
                            return analyze(helper,{p[0]:values[i] if i<len(values) else {} for i,p in enumerate(_parameters(helper.ast))})
                        imported=value.get('importPath','')+'.'+name.rsplit('.',1)[-1] if receiver is not None and value.get('importPath') else scope.get(_text(callable),{}).get('importPath')
                        if _framework(imported or name):
                            return asserted(node,values,offset) if re.search('assert|expect',name,re.I) else combine(values)
                        if re.match(r'^(?:(?:java\.util|System\.Collections\.Generic|std)\.)?(?:List|Map|Set|ArrayList|Optional|IEnumerable|Dictionary|String|string|str|Vec|vector)(?:<[^>]+>)?\.',name):
                            return value
                        interfaces.add(name)
                        signature=construct or self.language=='python' and name.rsplit('.',1)[-1][:1].isupper() or name in self.known_types or name.endswith(('.new','.default'))
                        dynamic=receiver is not None and not value.get('hint') and (re.match(r'^[a-z_$]',_text(receiver)) or receiver.type in _TEST_CALLS)
                        if callable is not None and callable.type in _TEST_CALLS:dynamic=True
                        if dynamic:reasons.add('接收者或调用目标未确认：'+name)
                        loc=self._location(owner.file,start=node.start_byte+offset,end=node.end_byte+offset)
                        call_id=(owner.file,loc.start_byte,loc.end_byte,name)
                        input_value=combine([value,*values])
                        facts.append({'consumer':name,'construct':bool(signature),'dynamic':bool(dynamic),'callId':call_id,
                                      'inputs':input_value['origins'],'inputCalls':input_value['callIds'],
                                      'producer':value.get('producer'),'asserted':False,
                                      'filePath':owner.file,'startLine':loc.start_line,'endLine':loc.end_line})
                        if construct:
                            anonymous=next((c for c in node.named_children if c.type=='class_body'),None)
                            if anonymous is not None:expr(anonymous,offset)
                        return {'producer':name,'callIds':list(dict.fromkeys(input_value['callIds']+[call_id])),
                                'origins':list(dict.fromkeys(input_value['origins']+[name])),
                                **({'hint':re.sub(r'\.(?:new|default)$','',name)} if signature else {})}
                    if node.type in {'assert_statement','comparison_operator'}:
                        return asserted(node,[expr(n,offset) for n in node.named_children],offset)
                    if node.type in {'return_statement','yield','return_expression'}:
                        value=expr(node.named_children[-1],offset) if node.named_children else {}
                        returned.append(value);return value
                    if node.type in {'if_statement','if_expression'}:
                        expr(_field(node,'condition'),offset);before=dict(scope);states=[]
                        for branch in [_field(node,'consequence') or _field(node,'body'),_field(node,'alternative')]:
                            scope.clear();scope.update(before);expr(branch,offset);states.append(dict(scope))
                        scope.clear()
                        for name,value in states[0].items():
                            other=states[1].get(name)
                            if value==other:scope[name]=value
                            elif other is not None:
                                merged=combine([value,other]);merged.pop('hint',None);merged.pop('producer',None)
                                for field in ['hint','producer']:
                                    if value.get(field)==other.get(field) and field in value:merged[field]=value[field]
                                scope[name]={**merged,'variable':name}
                        return {}
                    if node.type in {'switch_statement','switch_expression'}:
                        expr(_field(node,'value') or _field(node,'expression'),offset)
                        before=dict(scope);block=_field(node,'body') or node
                        branches=[n for n in block.named_children if n.type in {'switch_section','switch_expression_arm','case_statement'}]
                        states=[before]
                        for branch in branches:
                            scope.clear();scope.update(before);expr(branch,offset);states.append(dict(scope))
                        scope.clear()
                        for name in set.intersection(*(set(state) for state in states)):
                            values=[state[name] for state in states];merged=combine(values)
                            for field in ['hint','producer']:
                                if not all(v.get(field)==values[0].get(field) for v in values):merged.pop(field,None)
                            scope[name]={**merged,'variable':name}
                        reasons.add('分支赋值需复核接收者与调用关系')
                        return {}
                    return combine(expr(child,offset) for child in node.named_children)

                value=expr(body or _field(owner.ast,'body') or _field(owner.ast,'value'))
                if exported is not None:
                    for name,v in scope.items():
                        if body is not None or name.startswith(('self.','this.')):exported[name]=v
                active.remove(id(owner))
                return returned[0] if returned and all(v==returned[0] for v in returned) else value if owner.ast.type=='lambda' else {}

            def include_fixture(symbol):
                if id(symbol) in fixture_stack:
                    reasons.add('fixture循环依赖：'+symbol.name);return {}
                if id(symbol) in fixture_values:return fixture_values[id(symbol)]
                fixture_stack.add(id(symbol));add_support(symbol)
                bindings={}
                for name,typename in _parameters(symbol.ast):
                    dependency=self._fixture_for(name,test,owners,reasons)
                    if dependency is not None:bindings[name]=include_fixture(dependency)
                    else:reasons.add('fixture依赖未确认：'+name)
                result={**analyze(symbol,bindings),'producer':'fixture:'+self._alias(symbol)}
                fixture_values[id(symbol)]=result;fixture_stack.remove(id(symbol))
                return result

            initial={}
            tags='\n'.join([test.tags,*[o.tags for o in owners]])
            parametrize=re.search(r'parametrize\s*\(\s*([\'"])(.*?)\1',tags,re.S)
            parameters={n.strip() for n in parametrize.group(2).split(',')} if parametrize else set()
            for name,typename in _parameters(test.ast):
                if self.language=='python' and name not in parameters:
                    symbol=self._fixture_for(name,test,owners,reasons)
                    if symbol is not None:initial[name]={**include_fixture(symbol),'variable':name}
                    else:reasons.add('未找到可见fixture或参数来源：'+name)
                elif typename and not re.fullmatch(r'int|float|double|bool|boolean|string|String|str|void|long|char|[iu]\d+',typename):
                    initial[name]={'hint':typename,'variable':name}
            if self.language=='python':
                usefixtures=re.findall(r'usefixtures\s*\((.*?)\)',tags,re.S)
                requested={n for text in usefixtures for n in re.findall(r'[\'"]([^\'"]+)[\'"]',text)}
                for symbol in self.symbols:
                    if self._fixture(symbol) and (re.search(r'autouse\s*=\s*True',symbol.tags) or self._alias(symbol) in requested) and self._fixture_for(self._alias(symbol),test,owners,reasons) is symbol:
                        start=len(facts);include_fixture(symbol)
                        for fact in facts[start:]:self.context_references.update(fact['consumer'].replace('::','.').split('.'))
                if re.search(r'\bindirect\s*=',tags):reasons.add('间接参数化需框架确认')
            excluded=re.compile(r'^(?:import_|using_|package_|preproc_|function_|method_|constructor_|class_|struct_|interface_|enum_|decorated_definition|attribute_item|mod_item|impl_item|use_declaration)')
            context_nodes=[n for n in self.files[test.file].root_node.named_children if not excluded.match(n.type)]
            for cls in reversed(owners):
                cls_body=_field(cls.ast,'body')
                context_nodes.extend(n for n in cls_body.named_children if n.type in {'field_declaration','expression_statement','static_initializer','static_block'}) if cls_body is not None else None
            globals={};start=len(facts)
            analyze(test,globals,globals,SimpleNamespace(type='block',named_children=context_nodes,text=b''))
            for fact in facts[start:]:
                fact['shared']=True
                self.context_references.update(fact['consumer'].replace('::','.').split('.'))
            for name,value in globals.items():initial.setdefault(name,value)
            for symbol in self.symbols:
                if symbol.kind=='function' and self._lifecycle(symbol) and (any(symbol.owner is o for o in owners) or symbol.owner is None and symbol.file==test.file):
                    add_support(symbol);start=len(facts)
                    analyze(symbol,initial,initial)
                    for fact in facts[start:]:self.context_references.update(fact['consumer'].replace('::','.').split('.'))
            for provider in re.findall(r'(?:MethodSource|TestCaseSource|MemberData)\s*\(([^)]*)\)',tags):
                for name in re.findall(r'[\'"]([^\'"]+)[\'"]',provider):
                    symbol=self._helper_for(name,test,owners)
                    if symbol is not None:add_support(symbol)
                    else:reasons.add('参数数据提供者未确认：'+name)
            analyze(test,initial)
            name=test.name;container=test.owner.qualified if test.owner else Path(test.file).parent.as_posix().replace('/','.')
            if self.language=='cpp':
                decl=next((n for n in _walk(_field(test.ast,'declarator')) if n.type=='function_declarator'),None)
                args=_field(decl,'parameters')
                if args is not None and len(args.named_children)>1:
                    container=_text(_field(args.named_children[0],'type'));name=_text(_field(args.named_children[1],'type')) or name
            selector=test.file+'::'+(container+'::' if container and container!='.' else '')+name
            parameterization='dynamic' if re.search(r'MethodSource|TestCaseSource|MemberData|indirect\s*=',tags) else 'static' if re.search(r'parametrize|ParameterizedTest|CsvSource|ValueSource|TestCase\(|InlineData|DataRow',tags) else 'none'
            ids=[]
            for symbol in selected.values():
                if any(symbol is t for t in self.tests):ids.append(symbol.case_id)
                else:
                    key=(symbol.file,symbol.name,symbol.location.start_line)
                    support[key]=SupportUnit(key,symbol.name,symbol.owner.qualified if symbol.owner else '',symbol.location,
                                             'fixture' if self._fixture(symbol) or self._lifecycle(symbol) else 'helper')
            self.calls[test.case_id]=tuple(f for f in facts if not f.get('shared'))
            if reasons or not interfaces or any(f.get('dynamic') for f in facts):self.review.add(test.case_id)
            self.diagnostics.extend(diagnostic('TEST_REVIEW',reason,test.location,blocking=False) for reason in sorted(reasons))
            cases.append(TestCase(test.case_id,test.file,container,name,test.location,parameterization,
                                  tuple(sorted(interfaces)),tuple(ids),selector))
        # 只扫描加载时会执行的上下文；普通helper/fixture函数体不提前执行
        for file,tree in self.files.items():
            tests=[t for t in self.tests if t.file==file]
            def visit(node):
                if any(t.syntax_start<=node.start_byte and node.end_byte<=t.syntax_end for t in tests):return
                if node.type in {'identifier','type_identifier','field_identifier','namespace_identifier'}:self.context_references.add(_text(node))
                if node.type in {'wildcard_import','preproc_if','preproc_ifdef'} or node.type in _TEST_CALLS and _text(_field(node,'function')) in {'eval','exec','__import__','getattr'}:
                    self.context_opaque=True
                for child in node.named_children:
                    if self.language=='python' and node.type=='function_definition' and child==_field(node,'body'):continue
                    visit(child)
            visit(tree.root_node)
        if len({case.case_id for case in cases})!=len(cases) or len({case.runtime_selector for case in cases})!=len(cases):
            raise ValueError('同一用例标识或测试选择器对应多个定义')
        return ParsedTests({'python':'pytest','java':'junit','csharp':'xunit','cpp':'gtest','rust':'cargo'}[self.language],
                           tuple(cases),tuple(support.values()),tuple(self.diagnostics))
