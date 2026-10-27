"""python的测试与源码语法规则；不执行被分析代码。"""

import ast

import builtins

import sys

from dataclasses import replace

from pathlib import PurePosixPath

from .common import Source, Value, Facts, build_case, combine

from ..models import Diagnostic, ParsedTests, SupportUnit, UnresolvedItem, stable_id

class TestPythonRoute:
    language = 'python'
    extensions = {'.py'}

    def parse(self, bundle):
        cases, support, evidence, unresolved, diagnostics = ([], [], [], [], [])
        frameworks = set()
        for file in bundle.files:
            if file.role == 'resource' or PurePosixPath(file.relative_path).suffix != '.py':
                continue
            try:
                source = Source(file, True)
                tree = ast.parse(source.raw, filename=file.relative_path)
            except (SyntaxError, UnicodeError, LookupError) as error:
                diagnostics.append(Diagnostic('PARSE_ERROR', str(error)))
                continue
            module = '.'.join(PurePosixPath(file.relative_path).with_suffix('').parts)
            aliases = {}
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        prefix = (node.module or '') + '.' if isinstance(node, ast.ImportFrom) else ''
                        aliases[alias.asname or alias.name.split('.')[0]] = prefix + alias.name
            declarations = []

            def visit(nodes, container, test_class=False, parent_class=None):
                for node in nodes:
                    if isinstance(node, ast.ClassDef):
                        name = container + '.' + node.name
                        is_unittest = any((ast.unparse(b).endswith(('TestCase', 'unittest.TestCase')) for b in node.bases))
                        is_test = node.name.startswith('Test') or is_unittest
                        loc = source.ast_location(node)
                        support.append(SupportUnit(stable_id(file.relative_path, name, loc.start_byte), node.name, container, loc, 'context'))
                        visit(node.body, name, is_test, node)
                        if is_unittest:
                            frameworks.add('unittest')
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fixture = any((ast.unparse(d).split('(', 1)[0].endswith(('fixture', 'yield_fixture')) for d in node.decorator_list))
                        test = file.role == 'test' and node.name.startswith('test') and (parent_class is None or test_class)
                        lifecycle = node.name in {'setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'setup_method', 'teardown_method', 'setup_class', 'teardown_class', 'setup_module', 'teardown_module', '__init__'}
                        kind = 'test' if test else 'fixture' if fixture or lifecycle else 'helper'
                        loc = source.ast_location(node)
                        identity = stable_id(file.relative_path, container, node.name, loc.start_byte)
                        declarations.append((node, container, kind, identity, loc, parent_class))
            visit(tree.body, module)
            local_support = []
            for node, container, kind, identity, loc, parent_class in declarations:
                if kind != 'test':
                    unit = SupportUnit(identity, node.name, container, loc, kind, parameters=tuple((a.arg for a in node.args.args if a.arg not in {'self', 'cls'})))
                    support.append(unit)
                    local_support.append(unit)
            for node, container, kind, identity, loc, parent_class in declarations:
                facts = Facts(identity)
                scope = {a.arg: Value(hint='$' + a.arg) for a in node.args.args + node.args.kwonlyargs}

                def spelling(name):
                    first, _, tail = name.partition('.')
                    return aliases.get(first, first) + ('.' + tail if tail else '')

                def expr(n, asserted=False):
                    if n is None:
                        return Value()
                    if isinstance(n, ast.Name):
                        return scope.get(n.id, Value(hint=spelling(n.id)))
                    if isinstance(n, ast.Call):
                        original = ast.unparse(n.func)
                        if isinstance(n.func, ast.Attribute):
                            receiver = expr(n.func.value)
                            prefix = receiver.hint or ('$' + n.func.value.id if isinstance(n.func.value, ast.Name) else '<dynamic>')
                            if isinstance(n.func.value, ast.Name) and n.func.value.id not in scope and (n.func.value.id not in aliases) and n.func.value.id[:1].islower():
                                prefix = '$' + n.func.value.id
                            name = prefix + '.' + n.func.attr
                        else:
                            name = scope[original].hint or '$' + original if original in scope else spelling(original)
                        args = [expr(a, asserted) for a in n.args] + [expr(a.value, asserted) for a in n.keywords]
                        if isinstance(n.func, ast.Attribute):
                            args.append(receiver)
                        simple = n.func.attr if isinstance(n.func, ast.Attribute) else original
                        assertion = simple.startswith('assert') or name.startswith(('pytest.raises', 'unittest.TestCase.assert'))
                        shadowed = any((isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == original for n in tree.body)) or original in aliases
                        external = name.split('.')[0] in set(sys.stdlib_module_names) | {'pytest', 'builtins'} or (isinstance(n.func, ast.Name) and hasattr(builtins, n.func.id) and (not shadowed) and (original not in scope))
                        if assertion:
                            value = combine(args)
                            facts.mark(value, 'asserted')
                            return value
                        if external:
                            if simple in {'eval', 'exec', 'getattr', 'setattr', '__import__'}:
                                diagnostics.append(Diagnostic('UNSUPPORTED_DYNAMIC', '动态解释/反射无法保证支持闭包完整', source.ast_location(n)))
                            return combine(args)
                        construct = isinstance(n.func, ast.Name) and original[:1].isupper()
                        dynamic = not isinstance(n.func, (ast.Name, ast.Attribute)) or '<dynamic>' in name or name.startswith('$')
                        value = facts.call(name, args, source.ast_location(n), construct, dynamic)
                        facts.rows[-1] = replace(facts.rows[-1], fact={**facts.rows[-1].fact, 'argument_names': [None for a in n.args] + [a.arg for a in n.keywords] + ([None] if isinstance(n.func, ast.Attribute) else [])})
                        if asserted:
                            facts.mark(value, 'asserted')
                        return value
                    if isinstance(n, ast.Attribute):
                        value = expr(n.value, asserted)
                        if value.hint and (not value.calls) and (value.hint not in {'self', 'cls'}):
                            name = value.hint + '.' + n.attr
                            if name.split('.')[0] in set(sys.stdlib_module_names) | {'pytest', 'builtins'}:
                                return value
                            return facts.access(name, value, source.ast_location(n))
                        return Value(value.origins, value.calls)
                    if isinstance(n, ast.Lambda):
                        return expr(n.body, asserted)
                    return combine((expr(child, asserted) for child in ast.iter_child_nodes(n)))

                def statements(nodes, assertion_scope=False):
                    for stmt in nodes:
                        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            diagnostics.append(Diagnostic('NESTED_TEST_SCOPE', '嵌套声明的调用范围尚不能完整展开', source.ast_location(stmt)))
                            continue
                        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                            value = expr(stmt.value, assertion_scope)
                            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                            for target in targets:
                                if isinstance(target, ast.Name):
                                    scope[target.id] = value
                                    facts.bind(target.id, value)
                        elif isinstance(stmt, ast.Assert):
                            facts.mark(expr(stmt.test), 'asserted')
                        elif isinstance(stmt, (ast.Return, ast.Yield)):
                            facts.mark(expr(stmt.value, assertion_scope), 'returned')
                        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                            exception = any(('raises' in ast.unparse(item.context_expr) or 'assertRaises' in ast.unparse(item.context_expr) for item in stmt.items))
                            for item in stmt.items:
                                expr(item.context_expr)
                            statements(stmt.body, assertion_scope or exception)
                        elif isinstance(stmt, ast.If):
                            expr(stmt.test, assertion_scope)
                            before = dict(scope)
                            statements(stmt.body, assertion_scope)
                            left = dict(scope)
                            scope.clear()
                            scope.update(before)
                            statements(stmt.orelse, assertion_scope)
                            for key in left.keys() | scope.keys():
                                a, b = (left.get(key, Value()), scope.get(key, Value()))
                                joined = combine([a, b])
                                scope[key] = Value(joined.origins, joined.calls, a.hint if a.hint == b.hint else None)
                        elif isinstance(stmt, ast.Expr):
                            expr(stmt.value, assertion_scope)
                        else:
                            for child in ast.iter_child_nodes(stmt):
                                if isinstance(child, ast.stmt):
                                    statements([child], assertion_scope)
                                else:
                                    expr(child, assertion_scope)
                for d in node.decorator_list:
                    expr(d)
                for default in node.args.defaults + node.args.kw_defaults:
                    expr(default)
                statements(node.body)
                evidence.extend(facts.rows)
                if kind == 'test':
                    frameworks.add('pytest')
                    params = 'static' if any(('parametrize' in ast.unparse(d) for d in node.decorator_list)) else 'none'
                    class_names = container[len(module):].strip('.').split('.') if container != module else []
                    selector = file.relative_path + '::' + '::'.join(class_names + [node.name])
                    lifecycle_names = {'setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'setup_method', 'teardown_method', 'setup_class', 'teardown_class', 'setup_module', 'teardown_module', '__init__'}
                    param_names = set()
                    for d in node.decorator_list:
                        if isinstance(d, ast.Call) and 'parametrize' in ast.unparse(d.func) and d.args:
                            try:
                                arg = ast.literal_eval(d.args[0])
                                param_names.update((v.strip() for v in arg.split(',') if v.strip())) if isinstance(arg, str) else param_names.update(arg)
                            except (ValueError, TypeError, SyntaxError):
                                pass
                    fixture_names = tuple((a.arg for a in node.args.args if a.arg not in {'self', 'cls'} | param_names))
                    autouse = {uid for func, _, k, uid, _, _ in declarations if k == 'fixture' and any((isinstance(d, ast.Call) and any((kw.arg == 'autouse' and isinstance(kw.value, ast.Constant) and (kw.value.value is True) for kw in d.keywords)) for d in func.decorator_list))}
                    deps = tuple((u.unit_id for u in local_support if u.kind == 'fixture' and ((u.name in lifecycle_names or u.unit_id in autouse) and u.container in {container, module} or u.name in fixture_names)))
                    case = build_case(source, node.name, container, loc, params, selector, facts.rows, deps)
                    cases.append(replace(case, fixture_names=fixture_names))
                    for e in facts.rows:
                        if e.fact['dynamic']:
                            unresolved.append(UnresolvedItem(stable_id(e.evidence_id, 'unknown'), (identity,), '动态接收者或调用：' + e.fact['name'], (e.evidence_id,), True))
            module_actions = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)) and (not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)))]
            if module_actions:
                diagnostics.append(Diagnostic('MODULE_INITIALIZATION', '模块初始化执行语句的必要上下文尚未完整提取', source.ast_location(module_actions[0])))
            shared = set()
            for assignment in tree.body:
                if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and isinstance(assignment.value, (ast.Call, ast.List, ast.Dict, ast.Set)):
                    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                    shared.update((t.id for t in targets if isinstance(t, ast.Name)))
            for variable in shared:
                users = [uid for func, _, kind, uid, _, _ in declarations if kind == 'test' and any((isinstance(n, ast.Name) and n.id == variable for n in ast.walk(func)))]
                if len(users) > 1:
                    diagnostics.append(Diagnostic('SHARED_TEST_STATE', '多个测试显式复用模块对象，尚未证明每例重置：' + variable, blocking=True, related_ids=tuple(users)))
        framework = 'pytest' if 'pytest' in frameworks else 'unittest' if frameworks else 'unknown'
        return ParsedTests(framework, tuple(cases), tuple(support), tuple(evidence), tuple(unresolved), tuple(diagnostics))

import ast

import builtins

import json

import sys

from pathlib import PurePosixPath

from .common import SourceBaseRoute, SourceText, diagnostic, covers

from ..models import ParsedFile, Reference

class SourcePythonRoute(SourceBaseRoute):
    language = 'python'
    extensions = ('.py', '.pyi')

    def __init__(self):
        super().__init__()
        self.external_roots = set(sys.stdlib_module_names) | {'builtins'}
        self.star_imports = {}

    def _resolve(self, ref, inventory):
        targets, spelling = super()._resolve(ref, inventory)
        if targets:
            return targets, spelling
        if spelling.endswith('.*'):
            targets = [s for s in inventory.symbols.values() if s.kind == 'file' and s.qualified_name == spelling[:-2]]
        elif '.' not in spelling:
            names = {base + '.' + spelling for base in self.star_imports.get(ref.location.file_path, ())}
            targets = [s for s in inventory.symbols.values() if s.qualified_name in names]
        return targets, spelling

    def _import_module(self, ref, inventory):
        name = ref.spelling.removesuffix('.*')
        modules = [s for s in inventory.symbols.values() if s.kind == 'file' and
                   (name == s.qualified_name or name.startswith(s.qualified_name + '.'))]
        return max(modules, key=lambda s: len(s.qualified_name), default=None)

    def _initialization_modules(self, ref, inventory):
        module = self._import_module(ref, inventory)
        if module is None:
            return []
        # 子模块的导入也可能执行父包__init__.py，不能漏掉注册等初始化行为。
        return [s for s in inventory.symbols.values() if s.kind == 'file' and
                (s.symbol_id == module.symbol_id or
                 (s.locations[0].file_path.endswith('/__init__.py') and module.qualified_name.startswith(s.qualified_name + '.')))]

    def _module_initializes(self, module, inventory, visiting=frozenset()):
        file = module.locations[0].file_path
        if file in visiting:
            return False
        visiting = visiting | {file}
        tree = ast.parse(self.sources[file].raw)
        # 这里只认可可直接检查的纯定义／数据，不执行被分析工程。
        def simple_value(node):
            if node is None or isinstance(node, (ast.Constant, ast.Name)):
                return True
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return all(simple_value(x) for x in node.elts)
            if isinstance(node, ast.Dict):
                return all(x is not None for x in node.keys) and all(simple_value(x) for x in node.keys + node.values)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
                return isinstance(node.operand, ast.Constant)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'len' and len(node.args) == 1 and not node.keywords:
                shadowed = any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == 'len' or
                               isinstance(n, ast.Name) and n.id == 'len' and isinstance(n.ctx, ast.Store) or
                               isinstance(n, ast.alias) and (n.asname or n.name) == 'len' for n in ast.walk(tree))
                try:
                    value = ast.literal_eval(node.args[0])
                    return not shadowed and isinstance(value, (str, bytes, list, tuple, set, dict))
                except (ValueError, TypeError):
                    pass
            return False
        def simple_definition(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                args += [a for a in [node.args.vararg, node.args.kwarg] if a is not None]
                return not node.decorator_list and all(simple_value(x) for x in node.args.defaults + node.args.kw_defaults) and all(
                    simple_value(a.annotation) for a in args) and simple_value(node.returns)
            if isinstance(node, ast.ClassDef):
                return not node.decorator_list and not node.bases and not node.keywords and all(simple_definition(x) for x in node.body)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                return all(isinstance(t, ast.Name) for t in targets) and simple_value(node.value) and (not isinstance(node, ast.AnnAssign) or simple_value(node.annotation))
            return isinstance(node, ast.Pass) or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                loc = self.sources[file].location(self.sources[file].ast_byte(node.lineno, node.col_offset),
                                                  self.sources[file].ast_byte(node.end_lineno, node.end_col_offset))
                refs = [r for r in inventory.files[file].references if r.kind == 'import' and r.location == loc]
                for ref in refs:
                    modules = self._initialization_modules(ref, inventory)
                    if not modules or any(self._module_initializes(m, inventory, visiting) for m in modules):
                        return True
            elif not simple_definition(node):
                return True
        return False

    def _unit_context(self, unit, inventory):
        all_refs = super().dependency_references(unit, inventory)
        references = [r for r in all_refs if r.kind != 'import']
        dynamic = any(r.kind == 'dynamic' for r in references)
        imports = []
        for ref in (r for r in all_refs if r.kind == 'import'):
            if ref.scope_id is not None and ref.scope_id not in unit.member_symbol_ids:
                continue
            modules = self._initialization_modules(ref, inventory)
            inside = any(covers(f.location, ref.location) for f in unit.fragments)
            targets = [t for r in references for t in self._resolve(r, inventory)[0]]
            prefix = ref.spelling.removesuffix('.*')
            used = any(t.qualified_name == prefix or t.qualified_name.startswith(prefix + '.') for t in targets)
            if ref.spelling.endswith('.*'):
                used = any(t.qualified_name.startswith(prefix + '.') for t in targets)
            initialization = [m for m in modules if self._module_initializes(m, inventory)]
            ambiguous_star = ref.spelling.endswith('.*') and any(
                '.' not in r.spelling and r.kind in {'call', 'global', 'type'} and len(self._resolve(r, inventory)[0]) != 1
                for r in references)
            if inside or not modules or dynamic or ambiguous_star:
                references.append(ref)
            elif initialization:
                references.extend(Reference(m.qualified_name + '.*', 'initialization', ref.scope_id, ref.location) for m in initialization)
            if inside or not modules or used or initialization or dynamic or ambiguous_star:
                imports.append(self.sources[ref.location.file_path].fragment(ref.location).source)
        return tuple(dict.fromkeys(references)), tuple(dict.fromkeys(imports))

    def dependency_references(self, unit, inventory):
        return self._unit_context(unit, inventory)[0]

    def expand_unit(self, symbol, inventory):
        unit = super().expand_unit(symbol, inventory)
        return replace(unit, imports=self._unit_context(unit, inventory)[1])

    def parse_file(self, file_path, content):
        try:
            source = SourceText(file_path, content, python=True)
            tree = ast.parse(content, filename=file_path)
            self.sources[file_path] = source
            self.aliases[file_path] = {}
            self.star_imports[file_path] = []
            path = PurePosixPath(file_path)
            parts = list(path.with_suffix('').parts)
            if parts[-1] == '__init__':
                parts.pop()
            module_name = '.'.join(parts) or '__init__'
            symbols, references, imports = ([], [], [])
            nodes, owner_nodes, local_names, local_types, scope_aliases = ({}, {}, {}, {}, {None: {}})
            module = self._symbol(source, path.name, module_name, 'file', None, 0, len(content))
            symbols.append(module)

            def location(node):
                decorators = getattr(node, 'decorator_list', ())
                line = decorators[0].lineno if decorators else node.lineno
                start = source.ast_byte(line, node.col_offset)
                end = source.ast_byte(node.end_lineno, node.end_col_offset)
                return source.location(start, end)

            def declare(node, owner=None, prefix=module_name):
                next_owner, next_prefix = (owner, prefix)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = 'class' if isinstance(node, ast.ClassDef) else 'method' if owner and owner.kind == 'class' else 'function'
                    loc = location(node)
                    signature = None if kind == 'class' else '(' + ast.unparse(node.args) + ')'
                    symbol = self._symbol(source, node.name, prefix + '.' + node.name, kind, owner.symbol_id if owner else None, loc.start_byte, loc.end_byte, signature)
                    symbols.append(symbol)
                    nodes[id(node)] = symbol
                    owner_nodes[symbol.symbol_id] = owner.symbol_id if owner else None
                    next_owner, next_prefix = (symbol, symbol.qualified_name)
                    scope_aliases[symbol.symbol_id] = {}
                    local_names[symbol.symbol_id] = set()
                    local_types[symbol.symbol_id] = {}
                    if hasattr(node, 'args'):
                        for arg in ast.walk(node.args):
                            if isinstance(arg, ast.arg):
                                local_names[symbol.symbol_id].add(arg.arg)
                                if arg.annotation:
                                    local_types[symbol.symbol_id][arg.arg] = ast.unparse(arg.annotation)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and (owner is None or owner.kind == 'class'):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            loc = location(node)
                            symbols.append(self._symbol(source, target.id, prefix + '.' + target.id, 'declaration', owner.symbol_id if owner else None, loc.start_byte, loc.end_byte))
                for child in ast.iter_child_nodes(node):
                    declare(child, next_owner, next_prefix)
            declare(tree)

            def aliases(scope):
                result = dict(scope_aliases[None])
                chain = []
                while scope:
                    chain.append(scope)
                    scope = owner_nodes.get(scope)
                for scope in reversed(chain):
                    result.update(scope_aliases.get(scope, {}))
                return result

            def bind(node, scope=None):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scope_aliases.setdefault(scope, {}).pop(node.name, None)
                if id(node) in nodes:
                    scope = nodes[id(node)].symbol_id
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    scope_aliases.setdefault(scope, {}).pop(node.id, None)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and scope:
                    local_names[scope].add(node.id)
                if isinstance(node, ast.Assign) and scope and isinstance(node.value, ast.Call):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and isinstance(node.value.func, (ast.Name, ast.Attribute)):
                            local_types[scope][target.id] = ast.unparse(node.value.func)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append(ast.get_source_segment(source.text, node) or ast.unparse(node))
                    package = list(PurePosixPath(file_path).parent.parts)
                    if isinstance(node, ast.ImportFrom):
                        if node.level:
                            package = package[:len(package) - node.level + 1]
                        else:
                            package = []
                        base = '.'.join(package + ([node.module] if node.module else []))
                    for alias in node.names:
                        target = alias.name if isinstance(node, ast.Import) else base + '.' + alias.name
                        key = alias.asname or alias.name.split('.')[0]
                        scope_aliases.setdefault(scope, {})[key] = target
                        if isinstance(node, ast.ImportFrom) and alias.name == '*':
                            self.star_imports[file_path].append(base)
                        references.append(Reference(target, 'import', scope, location(node)))
                for child in ast.iter_child_nodes(node):
                    bind(child, scope)
            bind(tree)
            by_id = {symbol.symbol_id: symbol for symbol in symbols}
            parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

            def builtin_name(name, scope):
                if name in aliases(scope) or name in local_names.get(scope, set()):
                    return False
                if any((s.name == name and s.owner_id is None for s in symbols)):
                    return False
                return hasattr(builtins, name)

            def spelling(node, scope):
                value = ast.unparse(node)
                first, _, tail = value.partition('.')
                mapping = aliases(scope)
                if first in ('self', 'cls') and scope:
                    current = by_id[scope]
                    if current.owner_id:
                        return by_id[current.owner_id].qualified_name + ('.' + tail if tail else '')
                if first in local_types.get(scope, {}):
                    first = local_types[scope][first]
                first = mapping.get(first, first)
                return first + ('.' + tail if tail else '')

            def add(node, value, kind, scope):
                references.append(Reference(value, kind, scope, location(node)))

            def scan(node, scope=None):
                if id(node) in nodes:
                    scope = nodes[id(node)].symbol_id
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    return
                if isinstance(node, ast.Call):
                    value = spelling(node.func, scope)
                    if not isinstance(node.func, (ast.Name, ast.Attribute)):
                        add(node, value, 'dynamic', scope)
                        scan(node.func, scope)
                    elif isinstance(node.func, ast.Name) and builtin_name(node.func.id, scope) and (node.func.id in {'eval', 'exec', 'getattr', 'setattr', '__import__', 'globals', 'locals'}):
                        add(node, value, 'dynamic', scope)
                    elif value == 'builtins.open' or (value == 'open' and builtin_name('open', scope)):
                        argument = node.args[0] if node.args else None
                        add(node, json.dumps(argument.value) if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else ast.unparse(argument) if argument else '<missing>', 'resource', scope)
                    elif isinstance(node.func, ast.Name) and builtin_name(node.func.id, scope):
                        add(node, 'builtins.' + node.func.id, 'external', scope)
                    elif isinstance(node.func, ast.Name) and node.func.id in local_names.get(scope, set()) and (node.func.id not in local_types.get(scope, {})):
                        add(node, value, 'dynamic', scope)
                    else:
                        add(node, value, 'call', scope)
                    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
                        scan(node.func.value, scope)
                    for child in node.args:
                        scan(child, scope)
                    for child in node.keywords:
                        scan(child.value, scope)
                    return
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id in local_names.get(scope, set()) or builtin_name(node.id, scope):
                        return
                    parent = parents.get(id(node))
                    if isinstance(parent, ast.Attribute):
                        return
                    kind = 'inheritance' if isinstance(parent, ast.ClassDef) and node in parent.bases else 'global'
                    add(node, spelling(node, scope), kind, scope)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    if isinstance(node.value, ast.Name) and node.value.id in {'self', 'cls'}:
                        return
                    add(node, spelling(node, scope), 'global', scope)
                    if not isinstance(node.value, ast.Name):
                        scan(node.value, scope)
                    return
                for child in ast.iter_child_nodes(node):
                    scan(child, scope)
            scan(tree)
            for node in tree.body:
                simple = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
                docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                if not simple and (not docstring):
                    self.fallback_files.add(file_path)
            return ParsedFile(file_path, source.sha256, tuple(symbols), tuple(dict.fromkeys(imports)), tuple(references), ())
        except (SyntaxError, UnicodeError, ValueError, LookupError) as error:
            return self._failed(file_path, content, error)
