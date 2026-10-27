"""原文位置、测试事实与完整源码单元的共享语法辅助。"""
from __future__ import annotations
import ast
import hashlib
import io
import json
import tokenize
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from ..models import *
class Source:

    def __init__(self, file: PublicFile, python=False):
        self.file, self.raw = (file, file.content_bytes)
        self.encoding = tokenize.detect_encoding(io.BytesIO(self.raw).readline)[0] if python else 'utf-8'
        self.codec = 'utf-8' if self.encoding == 'utf-8-sig' else self.encoding
        self.text = self.raw.decode(self.encoding)
        self.lines = self.text.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.raw.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def position(self, line, column):
        head = self.lines[line - 1].encode('utf-8')[:column].decode('utf-8')
        return self.offsets[line - 1] + len(head.encode(self.codec)) + (3 if line == 1 and self.raw.startswith(b'\xef\xbb\xbf') else 0)

    def location(self, start, end):
        return Location(self.file.relative_path, self.raw[:start].count(b'\n') + 1, self.raw[:max(start, end - 1)].count(b'\n') + 1, start, end)

    def ast_location(self, node):
        decorators = getattr(node, 'decorator_list', ())
        first = decorators[0] if decorators else node
        return self.location(self.position(first.lineno, max(0, first.col_offset - 1) if decorators else first.col_offset), self.position(node.end_lineno, node.end_col_offset))

@dataclass(frozen=True)
class Value:
    origins: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()
    hint: str | None = None

def combine(values):
    values = list(values)
    return Value(tuple(dict.fromkeys((x for v in values for x in v.origins))), tuple(dict.fromkeys((x for v in values for x in v.calls))))

class Facts:
    """各语言只将语法转成调用/传值事实；不查看业务实现推断内部依赖。"""

    def __init__(self, scope_id):
        self.scope_id, self.rows = (scope_id, [])

    def call(self, spelling, args, location, construct=False, dynamic=False):
        inputs = combine(args)
        identity = stable_id(self.scope_id, location.file_path, location.start_byte, location.end_byte, spelling)
        fact = {'scope_id': self.scope_id, 'op': 'call', 'name': spelling, 'inputs': list(inputs.origins), 'input_calls': list(inputs.calls), 'argument_hints': [v.hint for v in args], 'construct': construct, 'dynamic': dynamic, 'asserted': False, 'returned': False}
        self.rows.append(Evidence(identity, location, 'call', fact))
        origin = spelling if construct else spelling.rsplit('.', 1)[0] if '.' in spelling else spelling
        hint = origin.removesuffix('.new').removesuffix('.default') if construct else None
        return Value(tuple(dict.fromkeys(inputs.origins + (origin,))), inputs.calls + (identity,), hint)

    def mark(self, value, key):
        ids = set(value.calls)
        self.rows = [replace(e, fact={**e.fact, key: True}) if e.evidence_id in ids else e for e in self.rows]

    def bind(self, name, value):
        if value.calls:
            identity = value.calls[-1]
            self.rows = [replace(e, fact={**e.fact, 'assigned_to': list(dict.fromkeys(e.fact.get('assigned_to', []) + [name]))}) if e.evidence_id == identity else e for e in self.rows]

    def access(self, name, receiver, location):
        value = self.call(name, [receiver], location)
        self.rows[-1] = replace(self.rows[-1], kind='read', fact={**self.rows[-1].fact, 'op': 'read'})
        return Value(value.origins, value.calls, name)

def interfaces(rows):
    return tuple((InterfaceRef(stable_id(e.fact['scope_id'], e.fact['name']) if e.fact['dynamic'] else stable_id(e.fact['name']), e.fact['name'], e.fact['name'].rpartition('.')[0] or None, 'asserted' if e.fact['asserted'] else 'construct' if e.fact['construct'] else 'prepare', (e.evidence_id,), 'unknown' if e.fact['dynamic'] else 'resolved') for e in rows if e.kind in {'call', 'read'}))

def build_case(source, name, container, location, parameterization, selector, rows, support_ids=()):
    case_id = stable_id(source.file.relative_path, container, name, location.start_byte)
    refs = interfaces(rows)
    return TestCase(case_id, source.file.relative_path, container, name, location, parameterization, tuple(dict.fromkeys((r.interface_id for r in refs if r.role == 'asserted'))), refs, support_ids, selector)

def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)

class TestTreeRoute:
    """配置语法节点而非用正则切方法；语言各自实现测试识别。"""

    def __init__(self, grammar):
        from tree_sitter import Language, Parser
        self.parser = Parser(Language(grammar))

    def text(self, source, node):
        return source.raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''

    def classify(self, node, source, container):
        return None

    def prefix(self, root, source):
        return ''

    def call_name(self, node, source):
        return ('', None, False)

    def is_assertion(self, name):
        return name.rsplit('.', 1)[-1].startswith(('assert', 'Assert', 'EXPECT_', 'ASSERT_')) or name.startswith('Assert.')

    def parse(self, bundle):
        cases, support, evidence, unresolved, diagnostics = ([], [], [], [], [])
        frameworks = set()
        for file in bundle.files:
            if file.role == 'resource' or Path(file.relative_path).suffix.lower() not in self.extensions:
                continue
            try:
                source = Source(file)
            except UnicodeError as error:
                diagnostics.append(Diagnostic('PARSE_ERROR', str(error)))
                continue
            root = self.parser.parse(source.raw).root_node
            if root.has_error:
                diagnostics.append(Diagnostic('PARSE_ERROR', '语法树含错误节点：' + file.relative_path))
            declarations = []

            def visit(node, container=self.prefix(root, source)):
                next_container = container
                if node.type in self.namespace_nodes:
                    name = self.text(source, node.child_by_field_name('name'))
                    next_container = '.'.join(filter(None, [container, name.replace('::', '.')]))
                if node.type in self.type_nodes:
                    name = self.text(source, node.child_by_field_name('name'))
                    next_container = '.'.join(filter(None, [container, name]))
                    start = node.start_byte
                    previous = node.prev_named_sibling
                    while self.language == 'rust' and previous and previous.type == 'attribute_item':
                        start = previous.start_byte
                        previous = previous.prev_named_sibling
                    loc = source.location(start, node.end_byte)
                    support.append(SupportUnit(stable_id(file.relative_path, next_container, loc.start_byte), name, container, loc, 'context'))
                if self.language == 'rust' and node.type == 'impl_item':
                    name = self.text(source, node.child_by_field_name('type'))
                    next_container = '.'.join(filter(None, [container, name]))
                    loc = source.location(node.start_byte, node.end_byte)
                    support.append(SupportUnit(stable_id(file.relative_path, next_container, loc.start_byte), name, container, loc, 'context'))
                result = self.classify(node, source, container)
                if result:
                    name, kind, params, framework, case_container = result
                    if self.language == 'cpp' and kind != 'test' and name.isupper() and (node.child_by_field_name('type') is None):
                        diagnostics.append(Diagnostic('UNSUPPORTED_TEST_MACRO', '无法确认的宏测试声明：' + name, source.location(node.start_byte, node.end_byte)))
                    if params == 'dynamic' and self.language in {'java', 'csharp'}:
                        diagnostics.append(Diagnostic('DYNAMIC_TEST_DATA', '动态测试数据源或重复执行语义尚未完整解析', source.location(node.start_byte, node.end_byte)))
                    start = node.start_byte
                    previous = node.prev_named_sibling
                    while self.language == 'rust' and previous and (previous.type in {'attribute_item', 'line_comment', 'block_comment'}):
                        start = previous.start_byte
                        previous = previous.prev_named_sibling
                    loc = source.location(start, node.end_byte)
                    identity = stable_id(file.relative_path, case_container, name, loc.start_byte)
                    if kind == 'test' and file.role != 'test':
                        kind = 'helper'
                    declarations.append((node, name, kind, params, identity, loc, case_container))
                    if kind == 'test':
                        frameworks.add(framework)
                    return
                for child in node.named_children:
                    visit(child, next_container)
            visit(root)
            local_support = []
            for node, name, kind, params, identity, loc, container in declarations:
                if kind != 'test':
                    parameter_list = node.child_by_field_name('parameters')
                    parameter_names = []
                    if parameter_list:
                        for p in parameter_list.named_children:
                            n = p.child_by_field_name('name') or p.child_by_field_name('pattern') or p.child_by_field_name('declarator')
                            if n:
                                parameter_names.append(self.text(source, n))
                    unit = SupportUnit(identity, name, container, loc, kind, parameters=tuple(parameter_names))
                    support.append(unit)
                    local_support.append(unit)
            for node, name, kind, params, identity, loc, container in declarations:
                facts = Facts(identity)
                scope = {}
                parameters = node.child_by_field_name('parameters')
                if parameters:
                    for param in parameters.named_children:
                        n = param.child_by_field_name('name') or param.child_by_field_name('pattern') or param.child_by_field_name('declarator')
                        t = param.child_by_field_name('type')
                        if n:
                            scope[self.text(source, n)] = Value(hint=self.text(source, t) or '$' + self.text(source, n))

                def expr(n, src=source):
                    if n is None:
                        return Value()
                    if n.type in {'identifier', 'field_identifier'}:
                        return scope.get(self.text(src, n), Value(hint=self.text(src, n)))
                    if n.type in self.call_nodes:
                        called, receiver, construct = self.call_name(n, src)
                        if receiver:
                            value = expr(receiver, src)
                            prefix = value.hint or ('$' + self.text(src, receiver) if receiver.type == 'identifier' else '<dynamic>')
                            if receiver.type == 'identifier' and self.text(src, receiver) not in scope and self.text(src, receiver)[:1].islower():
                                prefix = '$' + self.text(src, receiver)
                            if prefix in {'self', 'this', 'super', 'base'}:
                                prefix = container
                            called = prefix + '.' + called
                        called = called.replace('::', '.')
                        if ('<' in called or '>' in called) and '<dynamic>' not in called:
                            diagnostics.append(Diagnostic('UNSUPPORTED_TYPE_DISPATCH', '复杂泛型/模板调用尚不能完整解析', src.location(n.start_byte, n.end_byte)))
                        if called.endswith(('.forName', '.getMethod', '.getDeclaredMethod', '.invoke', '.GetMethod', '.Invoke', '.CreateInstance')):
                            diagnostics.append(Diagnostic('UNSUPPORTED_REFLECTION', '反射不能保证公有支持闭包完整', src.location(n.start_byte, n.end_byte)))
                        args = n.child_by_field_name('arguments')
                        inputs = [expr(a, src) for a in args.named_children] if args else []
                        if receiver:
                            inputs.append(value)
                        if self.is_assertion(called):
                            value = combine(inputs)
                            facts.mark(value, 'asserted')
                            return value
                        if called.split('.')[0] in self.external_names:
                            return combine(inputs)
                        return facts.call(called or '<dynamic>', inputs, src.location(n.start_byte, n.end_byte), construct, '<dynamic>' in called or called.startswith('$') or (not called))
                    if n.type == 'macro_invocation' and self.language == 'rust':
                        macro = self.text(src, n.child_by_field_name('macro'))
                        tokens = next((c for c in n.named_children if c.type == 'token_tree'), None)
                        if tokens and macro in {'assert', 'assert_eq', 'assert_ne', 'debug_assert', 'debug_assert_eq', 'debug_assert_ne', 'vec', 'format', 'format_args', 'matches'}:
                            prefix = b'fn __probe(){ __assert('
                            wrapped = prefix + src.raw[tokens.start_byte + 1:tokens.end_byte - 1] + b'); }'
                            parsed = self.parser.parse(wrapped).root_node
                            if parsed.has_error:
                                diagnostics.append(Diagnostic('UNSUPPORTED_MACRO', '宏参数无法按表达式安全分析', src.location(n.start_byte, n.end_byte)))
                                return Value()

                            class View:
                                raw = wrapped

                                def location(inner, start, end):
                                    return src.location(start - len(prefix) + tokens.start_byte + 1, end - len(prefix) + tokens.start_byte + 1)
                            call = next((x for x in walk(parsed) if x.type == 'call_expression'))
                            args = call.child_by_field_name('arguments')
                            value = combine((expr(a, View()) for a in args.named_children))
                            if 'assert' in macro:
                                facts.mark(value, 'asserted')
                            return value
                        diagnostics.append(Diagnostic('UNSUPPORTED_MACRO', '未知宏：' + macro, src.location(n.start_byte, n.end_byte)))
                        return Value()
                    if n.type in {'variable_declarator', 'init_declarator', 'let_declaration'}:
                        target = n.child_by_field_name('name') or n.child_by_field_name('declarator') or n.child_by_field_name('pattern')
                        value = n.child_by_field_name('value')
                        if value is None:
                            value = next((c for c in n.named_children if c != target and c.type not in {'type_identifier', 'implicit_type', 'predefined_type', 'identifier'}), None)
                        result = expr(value, src)
                        if target:
                            owner = n.parent
                            t = owner.child_by_field_name('type') if owner else None
                            hint = self.text(src, t) if t else ''
                            if hint and hint not in {'var', 'auto'}:
                                result = Value(result.origins, result.calls, hint)
                            scope[self.text(src, target)] = result
                            facts.bind(self.text(src, target), result)
                        return result
                    if n.type == 'declaration' and self.language == 'cpp':
                        target = n.child_by_field_name('declarator')
                        t = n.child_by_field_name('type')
                        if target and target.type == 'identifier' and t:
                            typename = self.text(src, t).replace('::', '.')
                            value = Value(hint=typename) if t.type == 'primitive_type' or typename.split('.')[0] in self.external_names else facts.call(typename, [], src.location(n.start_byte, n.end_byte), True)
                            scope[self.text(src, target)] = value
                            return value
                    if n.type in {'assignment_expression', 'assignment_statement'}:
                        left = n.child_by_field_name('left')
                        right = n.child_by_field_name('right')
                        value = expr(right, src)
                        if left and left.type == 'identifier':
                            scope[self.text(src, left)] = value
                            facts.bind(self.text(src, left), value)
                        return value
                    if n.type in {'field_expression', 'member_access_expression', 'field_access', 'member_expression'}:
                        receiver = n.child_by_field_name('argument') or n.child_by_field_name('expression') or n.child_by_field_name('object') or n.child_by_field_name('value')
                        value = expr(receiver, src) if receiver else combine((expr(c, src) for c in n.named_children))
                        member = n.child_by_field_name('field') or n.child_by_field_name('name')
                        if member and value.hint and (not value.calls) and (value.hint not in {'this', 'self', 'base', 'super'}):
                            if value.hint.split('.')[0] in self.external_names:
                                return value
                            return facts.access(value.hint + '.' + self.text(src, member), value, src.location(n.start_byte, n.end_byte))
                        return Value(value.origins, value.calls)
                    value = combine((expr(c, src) for c in n.named_children))
                    if n.type == 'assert_statement':
                        facts.mark(value, 'asserted')
                    if n.type in {'return_statement', 'return_expression'}:
                        facts.mark(value, 'returned')
                    return value
                expr(node.child_by_field_name('body'))
                evidence.extend(facts.rows)
                if kind == 'test':
                    selector = (container + '.' + name).strip('.')
                    if self.language == 'rust':
                        selector = selector.replace('.', '::')
                    if self.language == 'cpp':
                        selector = container.rsplit('.', 1)[-1] + '.' + name
                    deps = tuple((u.unit_id for u in local_support if u.kind == 'fixture' and u.container in {container, ''}))
                    cases.append(build_case(source, name, container, loc, params, selector, facts.rows, deps))
                    for e in facts.rows:
                        if e.fact['dynamic']:
                            unresolved.append(UnresolvedItem(stable_id(e.evidence_id, 'unknown'), (identity,), '动态接收者：' + e.fact['name'], (e.evidence_id,), True))
        framework = next(iter(frameworks)) if len(frameworks) == 1 else 'unknown'
        if len(frameworks) > 1:
            diagnostics.append(Diagnostic('MIXED_FRAMEWORKS', '同一公有输入有多个测试框架'))
        return ParsedTests(framework, tuple(cases), tuple(support), tuple(evidence), tuple(unresolved), tuple(diagnostics))

TYPE_KINDS = {'class', 'struct', 'interface', 'enum'}

CALL_KINDS = {'function', 'method', 'constructor'}

def diagnostic(code, message, location=None, candidates=(), blocking=True):
    return Diagnostic(code, message, location, blocking, candidates=tuple(candidates))

def covers(outer: Location, inner: Location) -> bool:
    return outer.file_path == inner.file_path and outer.start_byte <= inner.start_byte <= inner.end_byte <= outer.end_byte

class SourceText:

    def __init__(self, file_path: str, raw: bytes, python=False):
        self.file_path, self.raw = (file_path, raw)
        self.encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if python else 'utf-8'
        self.text = raw.decode(self.encoding)
        self.codec = 'utf-8' if self.encoding == 'utf-8-sig' else self.encoding
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.lines = self.text.splitlines(keepends=True)
        self.offsets = [0]
        for line in raw.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def ast_byte(self, line: int, utf8_column: int) -> int:
        prefix = self.lines[line - 1].encode('utf-8')[:utf8_column].decode('utf-8')
        bom = 3 if line == 1 and self.raw.startswith(b'\xef\xbb\xbf') else 0
        return self.offsets[line - 1] + bom + len(prefix.encode(self.codec))

    def location(self, start: int, end: int) -> Location:
        if not 0 <= start <= end <= len(self.raw):
            raise ValueError('源码字节范围越界')
        first = self.raw[:start].count(b'\n') + 1
        last = self.raw[:max(start, end - 1)].count(b'\n') + 1
        return Location(self.file_path, first, last, start, end)

    def fragment(self, location: Location) -> SourceFragment:
        source = self.raw[location.start_byte:location.end_byte].decode(self.codec)
        return SourceFragment(location, source, self.sha256)

class SourceBaseRoute:
    """具体语法由子类提取；此处只处理实体归属、证据和单元扩展。"""

    def __init__(self):
        self.sources: dict[str, SourceText] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        self.fallback_files: set[str] = set()
        self.partial_names: set[str] = set()
        self.external_roots: set[str] = set()

    def _failed(self, file_path, raw, error):
        return ParsedFile(file_path, hashlib.sha256(raw).hexdigest(), (), (), (), (diagnostic('PARSE_ERROR', str(error)),))

    def _symbol(self, source, name, qualified, kind, owner, start, end, signature=None):
        location = source.location(start, end)
        identity = stable_id(self.language, source.file_path, qualified, kind, signature, start, end)
        return SourceSymbol(identity, None, name, qualified, kind, signature, owner, (location,))

    def expand_unit(self, symbol, inventory):
        root = symbol
        seen = set()
        while root.owner_id:
            if root.symbol_id in seen or root.owner_id not in inventory.symbols:
                raise ValueError('实体所属关系缺失或成环')
            seen.add(root.symbol_id)
            root = inventory.symbols[root.owner_id]
        file = root.locations[0].file_path
        if file in self.fallback_files:
            root = next((s for s in inventory.files[file].symbols if s.kind == 'file'))
        locations = list(root.locations)
        members = []
        for candidate in inventory.symbols.values():
            current, visited = (candidate, set())
            while current.owner_id and current.symbol_id not in visited:
                visited.add(current.symbol_id)
                current = inventory.symbols[current.owner_id]
            belongs = current.symbol_id == root.symbol_id or any((covers(loc, point) for loc in locations for point in candidate.locations))
            if belongs:
                members.append(candidate.symbol_id)
                for point in candidate.locations:
                    if not any((covers(loc, point) for loc in locations)):
                        locations.append(point)
        locations = sorted(set(locations), key=lambda l: (l.file_path, l.start_byte, -l.end_byte))
        locations = [l for i, l in enumerate(locations) if not any((j != i and covers(other, l) for j, other in enumerate(locations)))]
        fragments = tuple((self.sources[l.file_path].fragment(l) for l in locations))
        imports = tuple(dict.fromkeys((item for l in locations for item in inventory.files[l.file_path].imports)))
        kind = root.kind if root.kind in TYPE_KINDS | {'function', 'file'} else 'declaration'
        reason = 'FILE_FALLBACK：模块初始化不能安全拆分' if file in self.fallback_files else '完整函数或完整所属类型及类外实现'
        return SourceUnit(stable_id(self.language, root.qualified_name, [(l.file_path, l.start_byte, l.end_byte) for l in locations]), kind, self.language, root.qualified_name, tuple(sorted(set(members + [root.symbol_id]))), fragments, imports, reason)

    def _resolve(self, ref, inventory):
        spelling = ref.spelling.replace('::', '.').replace('->', '.')
        file = ref.location.file_path
        first, _, tail = spelling.partition('.')
        mapped = self.aliases.get(file, {}).get(first)
        if mapped:
            spelling = mapped + ('.' + tail if tail else '')
        symbols = list(inventory.symbols.values())
        candidates = [s for s in symbols if s.qualified_name == spelling]
        if not candidates and '.' in spelling:
            receiver, _, member = spelling.partition('.')
            types = [s for s in symbols if s.kind in TYPE_KINDS and s.name == receiver]
            if len(types) == 1:
                candidates = [s for s in symbols if s.qualified_name == types[0].qualified_name + '.' + member]
            if not candidates:
                # 全局对象的方法调用还需要对象本身（如mtx.lock()中的mtx）。
                candidates = [s for s in symbols if s.kind == 'declaration' and not s.owner_id and
                              s.name == receiver and any(l.file_path == file for l in s.locations)]
        scope = inventory.symbols.get(ref.scope_id)
        if not candidates and '.' not in spelling:
            current = scope
            while current:
                scoped = [s for s in symbols if s.owner_id == current.symbol_id and s.name == spelling]
                if scoped:
                    candidates = scoped
                    break
                current = inventory.symbols.get(current.owner_id)
            if not candidates:
                candidates = [s for s in symbols if not s.owner_id and s.name == spelling and any((l.file_path == file for l in s.locations))]
            if not candidates and self.language != 'python':
                prefix = scope.qualified_name.rpartition('.')[0] if scope else ''
                candidates = [s for s in symbols if s.name == spelling and (s.kind in TYPE_KINDS or (not s.owner_id and s.qualified_name.rpartition('.')[0] == prefix))]
        if ref.kind.startswith('call/'):
            arity = int(ref.kind.split('/')[1])
            functions = [s for s in candidates if s.kind in CALL_KINDS or s.kind in TYPE_KINDS]
            if functions:
                candidates = functions
            if len(candidates) > 1:
                same_arity = [s for s in candidates if s.signature is not None and (0 if s.signature.split(')', 1)[0] == '(' else len(s.signature.split(')', 1)[0][1:].split(','))) == arity]
                if same_arity:
                    candidates = same_arity
        if ref.kind == 'include':
            matches = [s for s in symbols if s.kind == 'file' and any((l.file_path == spelling or l.file_path.endswith('/' + spelling) for l in s.locations))]
            local = (Path(file).parent / spelling).as_posix()
            nearby = [s for s in matches if any((l.file_path == local for l in s.locations))]
            if nearby:
                matches = nearby
            if matches:
                candidates = matches
        return (list({s.symbol_id: s for s in candidates}.values()), spelling)

    def dependency_references(self, unit, inventory):
        refs = []
        for fragment in unit.fragments:
            parsed = inventory.files[fragment.location.file_path]
            refs.extend((r for r in parsed.references if covers(fragment.location, r.location) or r.kind in {'import', 'include'}))
        return tuple(dict.fromkeys(refs))

    def scan_dependencies(self, unit, inventory):
        internal, external, resources, diagnostics = ([], [], [], [])
        if unit.reason.startswith('FILE_FALLBACK'):
            diagnostics.append(diagnostic('FILE_FALLBACK', unit.reason, unit.fragments[0].location, blocking=False))
        for ref in self.dependency_references(unit, inventory):
            kind = ref.kind.split('/')[0]
            if kind == 'local':
                continue
            if kind == 'dynamic':
                diagnostics.append(diagnostic('UNSUPPORTED_DYNAMIC', '无法静态确认：' + ref.spelling, ref.location))
                continue
            if kind == 'resource':
                try:
                    value = json.loads(ref.spelling)
                    if not isinstance(value, str):
                        raise ValueError()
                    path = Path(value)
                    if path.is_absolute() or '..' in path.parts or '\\' in value:
                        raise ValueError()
                    target = (inventory.repo_root / path).resolve()
                    if not target.is_relative_to(inventory.repo_root):
                        raise ValueError()
                    exists = target.is_file()
                    resources.append(ResourceReference(path.as_posix(), ref.spelling, exists, ref.location))
                    if not exists:
                        diagnostics.append(diagnostic('RESOURCE_NOT_FOUND', '资源文件不存在：' + value, ref.location))
                except (ValueError, OSError):
                    resources.append(ResourceReference(None, ref.spelling, None, ref.location))
                    diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '资源路径不能静态确认', ref.location))
                continue
            targets, spelling = self._resolve(ref, inventory)
            if len(targets) == 1:
                target = targets[0]
                edge_kind = kind if kind in {'call', 'type', 'inheritance', 'global', 'initialization', 'decoration', 'include'} else 'initialization'
                internal.append(InternalDependency(target, edge_kind, ref.location, 'syntax'))
            elif len(targets) > 1:
                diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '引用目标不唯一：' + spelling, ref.location, [s.symbol_id for s in targets]))
            elif spelling.split('.')[0] in self.external_roots or kind == 'external' or (kind == 'include' and b'<' in self.sources[ref.location.file_path].raw[ref.location.start_byte:ref.location.end_byte]):
                external.append(ExternalDependency(spelling, None, ref.location))
            else:
                diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '未找到引用目标：' + spelling, ref.location))
        return DependencyScan(tuple(dict.fromkeys(internal)), tuple(dict.fromkeys(external)), tuple(dict.fromkeys(resources)), tuple(dict.fromkeys(diagnostics)))

class SourceTreeRoute(SourceBaseRoute):
    """三种大括号语言共享树遍历；语法节点集合由具体路由提供。"""

    def __init__(self, grammar):
        super().__init__()
        from tree_sitter import Language, Parser
        self.parser = Parser(Language(grammar))
        self.trees = {}

    def node_text(self, source, node):
        return source.raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''

    def has_callable_body(self, symbol):
        return any(n.type in self.method_nodes and
                   loc.start_byte <= n.start_byte < n.end_byte <= loc.end_byte and
                   (n.child_by_field_name('body') is not None or any(c.type == 'arrow_expression_clause' for c in n.named_children))
                   for loc in symbol.locations for n in walk(self.trees[loc.file_path]))

    def parse_file(self, file_path, content):
        try:
            source = SourceText(file_path, content)
            self.sources[file_path] = source
            root = self.parser.parse(content).root_node
            self.trees[file_path] = root
            self.aliases[file_path] = {}
            symbols, refs, imports, diagnostics = ([], [], [], [])
            if root.has_error:
                diagnostics.append(diagnostic('PARSE_ERROR', '语法树包含错误节点：' + file_path))
            module = self._symbol(source, Path(file_path).name, file_path, 'file', None, 0, len(content))
            symbols.append(module)
            namespace = self.namespace(root, source)

            def visit(node, owner=None, prefix=namespace):
                scope_owner, scope_prefix = (owner, prefix)
                declared = self.declaration(node, source, owner)
                if declared:
                    name, kind, signature = declared
                    qualified = '.'.join(filter(None, [prefix, name.replace('::', '.')]))
                    whole = node.parent if node.parent and node.parent.type in self.wrappers else node
                    symbol = self._symbol(source, name.split('::')[-1], qualified, kind, owner.symbol_id if owner else None, whole.start_byte, whole.end_byte, signature)
                    symbols.append(symbol)
                    if kind in TYPE_KINDS | CALL_KINDS:
                        scope_owner, scope_prefix = (symbol, qualified)
                    if kind in TYPE_KINDS and self.is_partial(node, source):
                        self.partial_names.add(qualified)
                if node.type in self.namespace_nodes:
                    name = self.node_text(source, node.child_by_field_name('name'))
                    scope_prefix = '.'.join(filter(None, [prefix, name.replace('::', '.')]))
                for child in node.named_children:
                    visit(child, scope_owner, scope_prefix)
            visit(root)
            self.collect_references(root, source, symbols, refs, imports, diagnostics)
            return ParsedFile(file_path, source.sha256, tuple(symbols), tuple(imports), tuple(refs), tuple(diagnostics))
        except (SyntaxError, UnicodeError, ValueError) as error:
            return self._failed(file_path, content, error)

    def namespace(self, root, source):
        return ''

    def is_partial(self, node, source):
        return False

    def signature(self, node, source):
        params = node.child_by_field_name('parameters')
        if params is None:
            return None

        def parameter(n):
            value = self.node_text(source, n.child_by_field_name('type'))
            declarator = n.child_by_field_name('declarator')
            if declarator:
                raw = self.node_text(source, declarator)
                identifiers = [x for x in walk(declarator) if x.type == 'identifier']
                if identifiers:
                    name = identifiers[-1]
                    start, end = (name.start_byte - declarator.start_byte, name.end_byte - declarator.start_byte)
                    raw = (source.raw[declarator.start_byte:name.start_byte] + source.raw[name.end_byte:declarator.end_byte]).decode('utf-8')
                value += raw
            return value or self.node_text(source, n)
        return '(' + ','.join((parameter(n) for n in params.named_children if 'parameter' in n.type)) + ')'

    def collect_references(self, root, source, symbols, refs, imports, diagnostics):

        def scope(node):
            hits = [s for s in symbols if s.kind != 'file' and covers(s.locations[0], source.location(node.start_byte, node.end_byte))]
            return min(hits, key=lambda s: s.locations[0].end_byte - s.locations[0].start_byte) if hits else None
        locals_by_scope, fields = ({}, {})
        for node in walk(root):
            if node.type in self.binding_nodes:
                name_node = node.child_by_field_name('name') or node.child_by_field_name('declarator')
                if name_node and name_node.type not in {'identifier', 'field_identifier'}:
                    name_node = next((n for n in walk(name_node) if n.type == 'identifier'), None)
                if name_node:
                    current = scope(node)
                    if current:
                        type_node = node.child_by_field_name('type')
                        parent = node.parent
                        while type_node is None and parent is not None and (parent.type not in self.method_nodes):
                            type_node = parent.child_by_field_name('type')
                            parent = parent.parent
                        name = self.node_text(source, name_node)
                        kind = self.node_text(source, type_node)
                        locals_by_scope.setdefault(current.symbol_id, {})[name] = kind

        def add(node, spelling, kind):
            current = scope(node)
            refs.append(Reference(spelling, kind, current.symbol_id if current else None, source.location(node.start_byte, node.end_byte)))
        consumed = set()
        for node in walk(root):
            if node.type in self.import_nodes:
                raw = self.node_text(source, node)
                imports.append(raw)
                spelling, alias, category = self.import_spec(node, source)
                if spelling:
                    if alias:
                        self.aliases[source.file_path][alias] = spelling
                    add(node, spelling, category)
                consumed.update(((n.start_byte, n.end_byte) for n in walk(node)))
            if node.type in self.dynamic_nodes:
                add(node, self.node_text(source, node), 'dynamic')
                diagnostics.append(diagnostic('UNSUPPORTED_DYNAMIC', '未求解预处理条件或函数宏：' + node.type, source.location(node.start_byte, node.end_byte)))
        for node in walk(root):
            if (node.start_byte, node.end_byte) in consumed:
                continue
            if node.type in self.call_nodes:
                spelling, receiver = self.call_name(node, source)
                current = scope(node)
                local = locals_by_scope.get(current.symbol_id if current else '', {})
                if receiver:
                    if receiver in self.self_names and current:
                        owner = next((s for s in symbols if s.symbol_id == current.owner_id), None)
                        receiver = owner.qualified_name if owner else '<dynamic>'
                    elif receiver in local:
                        receiver = local[receiver] or '<dynamic>'
                    spelling = receiver + '.' + spelling
                elif spelling in local:
                    spelling = '<dynamic>.' + spelling
                args = node.child_by_field_name('arguments')
                if spelling:
                    add(node, spelling, 'dynamic' if '<dynamic>' in spelling else f'call/{(len(args.named_children) if args else 0)}')
                for part in self.call_identifiers(node):
                    consumed.add((part.start_byte, part.end_byte))
                if self.is_dynamic_call(spelling):
                    add(node, spelling, 'dynamic')
        for node in walk(root):
            if (node.start_byte, node.end_byte) in consumed:
                continue
            if node.type not in self.identifier_nodes:
                continue
            parent = node.parent
            if parent is None:
                continue
            if parent.type not in {'template_type', 'generic_name'} and any((parent.child_by_field_name(field) == node for field in ('name', 'declarator', 'field'))):
                continue
            current = scope(node)
            name = self.node_text(source, node)
            local = locals_by_scope.get(current.symbol_id if current else '', {})
            if name in local or name in self.known_names:
                continue
            if any((s.name == name and s.symbol_id == (current.symbol_id if current else None) for s in symbols)):
                continue
            if parent.type in self.namespace_nodes or parent.type in self.ignored_identifier_parents:
                continue
            kind = 'type' if node.type in self.type_identifier_nodes or parent.child_by_field_name('type') == node else 'global'
            add(node, name, kind)

    def call_identifiers(self, node):
        target = node.child_by_field_name('function') or node.child_by_field_name('name') or node.child_by_field_name('type')
        receiver = node.child_by_field_name('object')
        return [n for target_node in (target, receiver) if target_node for n in walk(target_node) if n.type in self.identifier_nodes | {'field_identifier'}]

    def is_dynamic_call(self, spelling):
        return False
