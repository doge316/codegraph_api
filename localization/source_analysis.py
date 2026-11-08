"""源码补充分析：本地语法、符号匹配、完整单元与项目内外依赖规则。"""
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from .codegraph import CodeGraph, GraphError
from .models import *
from .source_text import SourceText, CALL_KINDS, TYPE_KINDS, covers, diagnostic, checked_content, location, key_id
from pathlib import PurePosixPath
from .models import Reference, SourceUnit, stable_id

class PythonSourceRules:
    def _import_references(self, ref):
        names = self.reference_metadata.get(ref, {}).get('exportNames') if ref.spelling.endswith('.*') else None
        return [replace(ref, spelling=name) for name in names] if names is not None else [ref]

    def _resolve(self, ref, inventory):
        targets, spelling = super()._resolve(ref, inventory)
        if ref.kind == 'import':
            # Import lookup uses a module-qualified name; a same-file function
            # with the same short name cannot replace an imported module.
            targets = [s for s in targets if s.qualified_name == spelling]
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
        if self.file_initialization.get(file, True):
            return True
        for ref in inventory.files[file].references:
            if ref.kind != 'import':
                continue
            modules = self._initialization_modules(ref, inventory)
            if not modules or any(self._module_initializes(m, inventory, visiting | {file}) for m in modules):
                return True
        return False

    def _package_initialization(self, unit, inventory):
        # 入口直接位于子模块时，Python仍会先执行各级父包初始化。
        modules = {s.locations[0].file_path: s for s in inventory.symbols.values()
                   if s.kind == 'file' and s.locations}
        references = []
        for fragment in unit.fragments:
            path = PurePosixPath(fragment.location.file_path)
            own = modules.get(str(path))
            if unit.reason != 'INITIALIZATION_CONTEXT' and own is not None and self._module_initializes(own, inventory):
                references.append(Reference(own.qualified_name + '.*', 'initialization', None, fragment.location))
            for parent in path.parents:
                if parent == PurePosixPath('.'):
                    continue  # 工程搜索根目录不是其下模块的父包
                init = str(parent / '__init__.py')
                module = modules.get(init)
                if init != str(path) and module is not None and self._module_initializes(module, inventory):
                    references.append(Reference(module.qualified_name + '.*', 'initialization', None, fragment.location))
        return tuple(dict.fromkeys(references))

    def scan_dependencies(self, unit, inventory, *, references=None):
        scan = super().scan_dependencies(unit, inventory, references=references)
        problems = list(scan.diagnostics)
        if unit.reason == 'INITIALIZATION_CONTEXT':
            problems.append(diagnostic('INITIALIZATION_CONTEXT', '保留导入时执行的原文初始化片段及其直接依赖',
                                       unit.fragments[0].location, blocking=False))
        for fragment in unit.fragments:
            for parent in PurePosixPath(fragment.location.file_path).parents:
                if parent == PurePosixPath('.'):
                    continue
                init = str(parent / '__init__.py')
                parsed = inventory.files.get(init)
                if parsed is not None and any(d.blocking for d in parsed.diagnostics):
                    problems.append(diagnostic('PACKAGE_INITIALIZATION_INVALID',
                        '必要父包初始化无法完整解析：' + init, fragment.location))
        return replace(scan, diagnostics=tuple(dict.fromkeys(problems)))

    def _unit_context(self, unit, inventory):
        all_refs = super().dependency_references(unit, inventory)
        references = [r for r in all_refs if r.kind != 'import'] + list(self._package_initialization(unit, inventory))
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
            type_only = self.reference_metadata.get(ref, {}).get('typeOnly', False)
            if type_only and not used:
                continue
            initialization = [] if type_only else [m for m in modules if self._module_initializes(m, inventory)]
            ambiguous_star = ref.spelling.endswith('.*') and any(
                '.' not in r.spelling and r.kind in {'call', 'global', 'type'} and len(self._resolve(r, inventory)[0]) != 1
                for r in references)
            exports = unit.reason == 'INITIALIZATION_CONTEXT' and PurePosixPath(ref.location.file_path).name == '__init__.py'
            if inside or exports or not modules or dynamic or ambiguous_star or used:
                references.extend(self._import_references(ref))
            references.extend(Reference(m.qualified_name + '.*', 'initialization', ref.scope_id, ref.location) for m in initialization)
            if inside or exports or not modules or used or dynamic or ambiguous_star:
                context = self.reference_metadata.get(ref, {}).get('importContext')
                imports.append(context['source'] if context else self.sources[ref.location.file_path].fragment(ref.location).source)
        return tuple(dict.fromkeys(references)), tuple(dict.fromkeys(imports))

    def dependency_references(self, unit, inventory):
        if getattr(self, 'whole_file_scope', False):
            references = [expanded for ref in super().dependency_references(unit, inventory)
                          for expanded in (self._import_references(ref) if ref.kind == 'import' else [ref])] + list(self._package_initialization(unit, inventory))
            for ref in tuple(references):
                if ref.kind == 'import':
                    references.extend(Reference(m.qualified_name + '.*', 'initialization', ref.scope_id, ref.location)
                                      for m in self._initialization_modules(ref, inventory))
            return tuple(dict.fromkeys(references))
        return self._unit_context(unit, inventory)[0]

    def expand_unit(self, symbol, inventory):
        if symbol.kind == 'file' and not getattr(self, 'whole_file_scope', False) and symbol.locations[0].file_path in self.initialization_contents:
            file = symbol.locations[0].file_path
            fragments = self.initialization_contents[file] or (self.sources[file].fragment(self.sources[file].location(0, 0)),)
            members = tuple(s.symbol_id for s in inventory.symbols.values() if s.symbol_id == symbol.symbol_id or
                            any(covers(f.location, loc) for f in fragments for loc in s.locations))
            unit = SourceUnit(stable_id('initialization', file), 'declaration', self.language, symbol.qualified_name + '.*',
                              members, fragments, inventory.files[file].imports, 'INITIALIZATION_CONTEXT')
        else:
            unit = super().expand_unit(symbol, inventory)
        return replace(unit, imports=self._unit_context(unit, inventory)[1])



class SourceRules:
    """C++/C# 等语言的匹配、依赖和完整单元补全规则。"""

    def _resolve(self, ref, inventory):
        targets, spelling = super()._resolve(ref, inventory)
        if self.language in {'csharp', 'java', 'cpp'}:
            scope = inventory.symbols.get(ref.scope_id)
            prefix = scope.qualified_name.rpartition('.')[0] if scope else ''
            prefixes = []
            while prefix:
                prefixes.append(prefix)
                prefix = prefix.rpartition('.')[0]
            for prefix in prefixes:
                scoped = [s for s in inventory.symbols.values() if s.kind != 'file' and s.qualified_name == prefix + '.' + spelling]
                if scoped:
                    targets = scoped
                    break
            else:
                imported = {ns + '.' + spelling for ns in self.namespaces.get(ref.location.file_path, ())}
                scoped = [s for s in inventory.symbols.values() if s.kind != 'file' and s.qualified_name in imported]
                if scoped:
                    targets = scoped
            if not targets and '.' not in spelling:
                # Unscoped C++ enumerators still depend on their complete enum.
                targets = [s for s in inventory.symbols.values() if s.name == spelling and s.owner_id and
                           inventory.symbols[s.owner_id].kind == 'enum']
        generic_arity = self.reference_metadata.get(ref, {}).get('genericArity')
        if self.language == 'csharp' and generic_arity is not None:
            targets = [s for s in targets if s.kind not in TYPE_KINDS or
                       self.symbol_metadata(s).get('genericArity', generic_arity) == generic_arity]
        if ref.kind.startswith('call/') and len(targets) > 1:
            arity = int(ref.kind.split('/')[1])
            compatible = [s for s in targets if self._accepts_arity(s, arity)]
            if compatible:
                targets = compatible
            types = self.reference_metadata.get(ref, {}).get('argumentTypes', ())
            import re
            normalize = lambda t: re.sub(r'\b(?:const|volatile|struct|class)\b|\s+|[&]', '', t).replace('::', '.')
            typed = [s for s in targets if all(not actual or not expected or normalize(actual) == normalize(expected)
                     for actual, expected in zip(types, self.symbol_metadata(s).get('parameters', ())))]
            if typed:
                targets = typed
        if self.language == 'csharp' and not targets and spelling in {
            'ArgumentException', 'ArgumentNullException', 'ArgumentOutOfRangeException', 'DivideByZeroException',
            'InvalidOperationException', 'NotSupportedException', 'IndexOutOfRangeException', 'NullReferenceException', 'OverflowException'
        } and any(item.strip() == 'using System;' for item in inventory.files[ref.location.file_path].imports):
            spelling = 'System.' + spelling
        return targets, spelling

    def _accepts_arity(self, symbol, arity):
        row = self.symbol_metadata(symbol)
        parameters = row.get('parameters')
        return parameters is None or row.get('requiredParameters', len(parameters)) <= arity <= len(parameters)

    def dependency_references(self, unit, inventory):
        references = super().dependency_references(unit, inventory)
        if self.language != 'cpp' or getattr(self, 'whole_file_scope', False):
            return references
        # Includes remain verbatim import context. Resolve their referenced
        # types/functions/macros rather than expanding every header definition.
        references = [ref for ref in references if ref.kind != 'include' or
                      not self._resolve(ref, inventory)[0] or any(t.locations[0].file_path not in self.initialization_contents or
                      self.initialization_contents[t.locations[0].file_path]
                      for t in self._resolve(ref, inventory)[0])]
        for sid in unit.member_symbol_ids:
            row = self.metadata.get(sid, {})
            if row.get('conditions'):
                references.append(Reference('；'.join(row['conditions']), 'dynamic', sid,
                                            inventory.symbols[sid].locations[0]))
        return tuple(dict.fromkeys(references))

    def expand_unit(self, symbol, inventory):
        unit = super().expand_unit(symbol, inventory)
        if self.language != 'cpp':
            return unit
        if symbol.kind == 'file':
            return replace(unit, reason='FILE_FALLBACK：头文件存在初始化或必须整体保留的编译指令')
        fragments = list(unit.fragments)
        files = {name: source.raw for name, source in self.sources.items()}
        for sid in unit.member_symbol_ids:
            for point in inventory.symbols[sid].locations:
                row = self.metadata_by_location.get((point.file_path, point.start_byte, point.end_byte), {})
                for context in row.get('contexts', ()):
                    loc = checked_content(context, files)
                    if not any(covers(f.location, loc) for f in fragments):
                        fragments.append(self.sources[loc.file_path].fragment(loc))
        return replace(unit, fragments=tuple(sorted(set(fragments), key=lambda f:(f.location.file_path,f.location.start_byte))))


class SourceBaseRoute:
    """处理语法事实的实体归属、引用匹配和完整源码单元扩展。"""

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
        candidates = [s for s in symbols if s.qualified_name == spelling and (s.kind != 'file' or ref.kind in {'import', 'include'})]
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
                same_arity = [s for s in candidates if self._accepts_arity(s, arity)]
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

    def scan_dependencies(self, unit, inventory, *, references=None):
        internal, external, resources, diagnostics = ([], [], [], [])
        if unit.reason.startswith('FILE_FALLBACK'):
            diagnostics.append(diagnostic('FILE_FALLBACK', unit.reason, unit.fragments[0].location, blocking=False))
        for ref in self.dependency_references(unit, inventory) if references is None else references:
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
                # Unknown argument types do not justify choosing one overload.
                # Keep the viable overload bodies and retain the uncertainty.
                if kind == 'call' and len({t.qualified_name for t in targets}) == 1 and all(t.kind in CALL_KINDS for t in targets):
                    internal.extend(InternalDependency(t, 'call', ref.location, 'syntax') for t in targets)
                elif len({self.expand_unit(t, inventory).unit_id for t in targets}) == 1:
                    internal.append(InternalDependency(targets[0], kind if kind in {'type', 'global'} else 'call', ref.location, 'syntax'))
                diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '引用目标不唯一：' + spelling, ref.location, [s.symbol_id for s in targets]))
            elif spelling.split('.')[0] in self.external_roots or kind == 'external' or (kind == 'include' and b'<' in self.sources[ref.location.file_path].raw[ref.location.start_byte:ref.location.end_byte]):
                external.append(ExternalDependency(spelling, None, ref.location))
            else:
                diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '未找到引用目标：' + spelling, ref.location))
        return DependencyScan(tuple(dict.fromkeys(internal)), tuple(dict.fromkeys(external)), tuple(dict.fromkeys(resources)), tuple(dict.fromkeys(diagnostics)))


class UpstreamSource(SourceRules, SourceBaseRoute):
    def __init__(self, language, graph=None):
        super().__init__()
        self.language, self.extensions = language, EXTENSIONS[language]
        self.graph = graph or CodeGraph({})
        self.root = None
        self.metadata, self.metadata_by_location, self.file_initialization = {}, {}, {}
        self.initialization_contents, self.reference_metadata, self.namespaces = {}, {}, {}
        self.import_contents = {}
        self.definition_ranges, self.internal_ranges, self.star_imports = set(), set(), {}
        self.external_roots = set(sys.stdlib_module_names) | {'builtins', 'std', 'System', 'java', 'javax', 'String', 'Object', 'Math', 'Console'}

    def bind(self, root):
        self.root = Path(root)
        # 索引准备由公开主流程负责；旧调用方仍可单独使用build_inventory
        if not getattr(self, 'graph_prepared', False):
            self.graph.prepare(root, self.language)
        paths, problems = source_files(self.root, self.language)
        if problems:
            raise ValueError('INVALID_PATH：源码清单含越界或目录链接')
        _syntax_files(self, paths)

    def has_callable_body(self, symbol):
        return self.metadata.get(symbol.symbol_id, {}).get('hasBody', True)

    def symbol_metadata(self, symbol):
        rows = [self.metadata_by_location[key] for loc in symbol.locations
                if (key := (loc.file_path, loc.start_byte, loc.end_byte)) in self.metadata_by_location]
        return {**(rows[0] if rows else {}), **{flag: any(row.get(flag, False) for row in rows) for flag in ('eager', 'hasBody', 'partial', 'internal')}}

    def parse_file(self, file_path, raw):
        if self.root is None:
            raise ValueError('源码路由尚未绑定工程根目录')
        source = self.sources[file_path]
        result = _syntax_result(self, file_path, raw)
        self.file_initialization[file_path] = result.get('initialization', True)
        self.import_contents[file_path] = tuple(
            (checked_content(row, {file_path: raw}), row['source'])
            for row in result.get('imports', ()) if row['filePath'] == file_path)
        if 'initializationContents' in result:
            self.initialization_contents[file_path] = tuple(
                source.fragment(checked_content(row, {file_path: raw}))
                for row in result['initializationContents'] if row['filePath'] == file_path)
        module = file_path.rsplit('.', 1)[0].replace('/', '.').removesuffix('.__init__')
        aliases = self.aliases[file_path] = {}
        declarations = [r for r in result['declarations'] if r['filePath'] == file_path]
        ids = {}
        symbols = []
        whole = self._symbol(source, Path(file_path).name, module, 'file', None, 0, len(raw))
        symbols.append(whole)
        for row in declarations:
            loc = checked_content(row, {file_path: raw})
            qualified = row.get('qualifiedName') or row['key']['name']
            if self.language == 'python':
                qualified = module + '.' + qualified
            symbol = self._symbol(source, row['key']['name'], qualified, row['kind'], None,
                                  loc.start_byte, loc.end_byte, row.get('signature'))
            ids[(key_id(row['key']), loc.start_byte, loc.end_byte)] = symbol.symbol_id
            symbols.append(symbol)
            self.metadata[symbol.symbol_id] = row
            self.metadata_by_location[(file_path, loc.start_byte, loc.end_byte)] = row
            if row.get('partial'):
                self.partial_names.add(qualified)
            if row.get('hasBody'):
                self.definition_ranges.add((file_path, loc.start_byte, loc.end_byte))
            if row.get('internal'):
                self.internal_ranges.add((file_path, loc.start_byte, loc.end_byte))
        by_id = {s.symbol_id: s for s in symbols}
        for row in declarations:
            sid = ids[key_id(row['key']), row['range']['startByte'], row['range']['endByte']]
            owner_range = row.get('containerRange') or {}
            owner = ids.get((key_id(row['container']), owner_range.get('startByte'), owner_range.get('endByte'))) if row.get('container') else None
            by_id[sid] = replace(by_id[sid], owner_id=owner)
        refs = []
        for fact in result['facts']:
            if fact['filePath'] != file_path:
                continue
            loc = location(file_path, fact, raw)
            kind = fact.get('referenceKind', 'call' if fact['kind'] == 'calls' else 'global')
            spelling = fact['consumer']
            if fact.get('variable') and fact.get('producer') and kind == 'import':
                if fact.get('importContext'):
                    checked_content(fact['importContext'], {file_path: raw})
                aliases[fact['variable']] = fact['producer']
                if fact['variable'] == '*':
                    self.star_imports.setdefault(file_path, set()).add(spelling.removesuffix('.*'))
            owner_range = fact.get('ownerRange') or {}
            scope = ids.get((key_id(fact['owner']), owner_range.get('startByte'), owner_range.get('endByte'))) if fact['owner'].get('kind') != 'file' else None
            refs.append(Reference(spelling, 'external' if fact.get('external') else kind, scope, loc))
            self.reference_metadata[refs[-1]] = fact
            if kind == 'import' and fact.get('namespace'):
                self.namespaces.setdefault(file_path, []).append(spelling.replace('::', '.'))
        imports = tuple(dict.fromkeys(r['source'] for r in result.get('imports', ()) if r['filePath'] == file_path))
        problems = tuple(diagnostic('PARSE_ERROR' if '语法树存在错误' in w else 'UPSTREAM_SOURCE_WARNING', w,
                                   source.location(0, len(raw)), blocking=True) for w in result['warnings'])
        return ParsedFile(file_path, source.sha256, tuple(by_id.values()), imports, tuple(dict.fromkeys(refs)), problems)



class PythonSource(PythonSourceRules, UpstreamSource):
    pass



SKIP_DIRECTORIES = {'.git', '.codegraph', 'node_modules', '.venv', 'venv', '__pycache__', 'obj', 'bin', 'build', 'dist', '.pytest_cache'}

EXTENSIONS = {'python': {'.py', '.pyi'}, 'java': {'.java'}, 'csharp': {'.cs'}, 'cpp': {'.h', '.hh', '.hpp', '.hxx', '.c', '.cc', '.cpp', '.cxx'}}

def source_files(root, language):
    result, problems = ([], [])
    for directory, subdirs, names in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(subdirs):
            if name in SKIP_DIRECTORIES:
                continue
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                problems.append(diagnostic('INVALID_PATH', '不跟随目录链接：' + path.relative_to(root).as_posix()))
            else:
                kept.append(name)
        subdirs[:] = kept
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix.lower() not in EXTENSIONS[language]:
                continue
            if not path.resolve().is_relative_to(root):
                problems.append(diagnostic('INVALID_PATH', '拒绝仓库外源码链接：' + path.relative_to(root).as_posix()))
            else:
                result.append(path)
    return (result, problems)

def build_inventory(repo_root, language, graph_symbols, route):
    root = repo_root.resolve(strict=True)
    if not root.is_dir() or language not in EXTENSIONS or route.language != language:
        raise ValueError('源码根目录或源语言路由不合法')
    paths, diagnostics = source_files(root, language)
    route.bind(root)
    files = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            files[relative] = route.parse_file(relative, path.read_bytes())
            diagnostics.extend(replace(d, location=Location(relative, 1, 1, 0, 0)) if d.location is None else d
                               for d in files[relative].diagnostics)
        except OSError as error:
            diagnostics.append(diagnostic('SOURCE_CHANGED', f'源码读取失败：{relative}：{error}'))
    symbols = {s.symbol_id: s for parsed in files.values() for s in parsed.symbols}
    if language == 'cpp':
        import re
        types = {s.qualified_name for s in symbols.values() if s.kind in TYPE_KINDS}
        for uid, symbol in list(symbols.items()):
            if symbol.kind not in CALL_KINDS | {'declaration'}:
                continue
            prefix, separator, member = symbol.qualified_name.rpartition('.')
            plain = re.sub(r'<[^<>]*>', '', prefix)
            qualified = plain + separator + member if prefix not in types and plain in types else symbol.qualified_name
            symbols[uid] = replace(symbol, qualified_name=qualified,
                                   signature=re.sub(r'\s+', '', symbol.signature) if symbol.signature else None)
    groups = {}
    for symbol in symbols.values():
        merge = language == 'csharp' and symbol.kind in TYPE_KINDS and (symbol.qualified_name in route.partial_names)
        merge = merge or (language == 'cpp' and symbol.kind in CALL_KINDS | TYPE_KINDS)
        if merge:
            internal = language == 'cpp' and any(((l.file_path, l.start_byte, l.end_byte) in route.internal_ranges for l in symbol.locations))
            scope_file = symbol.locations[0].file_path if internal else None
            groups.setdefault((symbol.qualified_name, symbol.signature, symbol.kind, scope_file), []).append(symbol)
    remap = {}
    for (name, signature, kind, scope_file), group in groups.items():
        if len(group) < 2:
            continue
        if language == 'cpp' and sum((any(((l.file_path, l.start_byte, l.end_byte) in route.definition_ranges for l in s.locations)) for s in group)) > 1:
            diagnostics.append(diagnostic('UNRESOLVED_REFERENCE', '同名同签名存在多个实现，不能直接合并：' + name, group[0].locations[0], [s.symbol_id for s in group]))
            continue
        retained_group = group
        if language == 'cpp' and kind in CALL_KINDS:
            definitions = [s for s in group if any((l.file_path, l.start_byte, l.end_byte) in route.definition_ranges for l in s.locations)]
            if len(definitions) == 1:
                implementation = definitions[0]
                def test_file(path):
                    parts = Path(path).parts
                    return any(p.lower() in {'test', 'tests', 'public_test', 'public_tests'} for p in parts[:-1]) or Path(path).name.lower().startswith('test_')
                if not any(test_file(l.file_path) for l in implementation.locations):
                    # 合并真实头文件声明与实现，但不把测试侧的同名原型当作实现片段。
                    retained_group = [s for s in group if s is implementation or not all(test_file(l.file_path) for l in s.locations)]
        locations = tuple(sorted({l for s in retained_group for l in s.locations}, key=lambda l: (l.file_path, l.start_byte)))
        identity = stable_id(language, name, signature, kind, [(l.file_path, l.start_byte, l.end_byte) for l in locations])
        merged = replace(group[0], symbol_id=identity, locations=locations)
        for symbol in group:
            symbols.pop(symbol.symbol_id)
            remap[symbol.symbol_id] = identity
        symbols[identity] = merged
    types = {s.qualified_name: s for s in symbols.values() if s.kind in TYPE_KINDS}
    for identity, symbol in list(symbols.items()):
        owner = remap.get(symbol.owner_id, symbol.owner_id)
        if language == 'cpp' and symbol.kind in CALL_KINDS | {'declaration'} and (not owner):
            type_owner = types.get(symbol.qualified_name.rpartition('.')[0])
            if type_owner:
                owner = type_owner.symbol_id
        symbols[identity] = replace(symbol, owner_id=owner)
    bindings = {}
    for identity, symbol in list(symbols.items()):
        if symbol.kind not in CALL_KINDS:
            continue
        candidates = {g.id: g for g in graph_symbols if g.name == symbol.name and any((g.file_path == l.file_path and g.start_line is not None and (l.start_line <= g.start_line <= l.end_line) for l in symbol.locations))}
        competitors = [s for s in symbols.values() if s.symbol_id != identity and s.name == symbol.name and any((a.file_path == b.file_path and a.start_line == b.start_line for a in s.locations for b in symbol.locations))]
        if len(candidates) == 1 and (not competitors):
            graph_id = next(iter(candidates))
            bindings[identity] = graph_id
            symbols[identity] = replace(symbol, graph_node_id=graph_id)
        else:
            diagnostics.append(diagnostic('GRAPH_SYMBOL_UNRESOLVED', '源码实体未能唯一关联图节点：' + symbol.qualified_name, symbol.locations[0], candidates))
    for file, parsed in list(files.items()):
        files[file] = replace(parsed, symbols=tuple((s for s in symbols.values() if any((l.file_path == file for l in s.locations)))), references=tuple((replace(r, scope_id=remap.get(r.scope_id, r.scope_id)) for r in parsed.references)))
    return SourceInventory(root, language, files, symbols, bindings, tuple(diagnostics))

def validate_inventory(inventory):
    diagnostics = list(inventory.diagnostics)
    paths, problems = source_files(inventory.repo_root, inventory.language)
    diagnostics.extend(problems)
    current = {path.relative_to(inventory.repo_root).as_posix(): path for path in paths}
    if set(current) != set(inventory.files):
        diagnostics.append(diagnostic('SOURCE_CHANGED', '分析期间源码文件清单发生变化'))
    for name, parsed in inventory.files.items():
        try:
            path = inventory.repo_root / name
            if not path.resolve().is_relative_to(inventory.repo_root):
                raise ValueError('源码链接越界')
            if hashlib.sha256(path.read_bytes()).hexdigest() != parsed.sha256:
                diagnostics.append(diagnostic('SOURCE_CHANGED', '分析期间源码内容变化：' + name))
        except (OSError, ValueError) as error:
            diagnostics.append(diagnostic('SOURCE_CHANGED', f'源码不再可读取：{name}：{error}'))
    return tuple(dict.fromkeys(diagnostics))


# 本模块的补充语法分析：使用项目已安装的Tree-sitter语法，不调用增强HTTP接口
import importlib
import re
from types import SimpleNamespace


_TYPES = {'class_definition', 'class_declaration', 'record_declaration', 'struct_declaration',
          'interface_declaration', 'class_specifier', 'struct_specifier', 'enum_specifier', 'enum_declaration'}
_FUNCTIONS = {'function_definition', 'method_declaration', 'constructor_declaration', 'operator_declaration'}
_CALLS = {'call', 'call_expression', 'method_invocation', 'invocation_expression', 'object_creation_expression'}
_IMPORTS = {'import_statement', 'import_from_statement', 'import_declaration', 'using_directive',
            'package_declaration', 'file_scoped_namespace_declaration', 'preproc_include'}
_KNOWN = set(dir(__import__('builtins'))) | {
    'self', 'cls', 'this', 'super', 'base', 'null', 'true', 'false', 'void', 'bool', 'double',
    'char', 'long', 'string', 'object', 'int32_t', 'size_t', 'var', 'auto'}


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
    current = _field(node, 'declarator')
    while current is not None and current.type not in {
        'identifier', 'field_identifier', 'qualified_identifier', 'operator_name', 'destructor_name'}:
        current = _field(current, 'declarator') or next(
            (n for n in current.named_children if n.type != 'parameter_list'), None)
    return current


def _params(node):
    params = _field(node, 'parameters')
    if params is None:
        decl = next((n for n in _walk(_field(node, 'declarator')) if n.type == 'function_declarator'), None)
        params = _field(decl, 'parameters')
    result = []
    for item in params.named_children if params is not None else ():
        binding = (_declarator(item) or _field(item, 'name') or _field(item, 'pattern') or
                   (item if item.type == 'identifier' else next((n for n in item.named_children if n.type == 'identifier'), None)))
        name = _text(binding)
        if name and name not in {'self', 'cls', '*', '/'}:
            result.append((name, _text(_field(item, 'type')), '=' in _text(item), item))
    return result


def _span(source, node=None, start=None, end=None):
    # Tree-sitter解析UTF-8；对外范围始终映射回原文件字节，兼容BOM和Python编码声明
    encoded = source.text.encode('utf-8')
    start = node.start_byte if start is None else start
    end = node.end_byte if end is None else end
    bom = 3 if source.raw.startswith(b'\xef\xbb\xbf') else 0
    first = len(encoded[:start].decode('utf-8').encode(source.codec)) + bom
    last = len(encoded[:end].decode('utf-8').encode(source.codec)) + bom
    loc = source.location(first, last)
    return {'filePath': source.file_path, 'source': source.fragment(loc).source,
            'range': {'startByte': first, 'endByte': last, 'startLine': loc.start_line, 'endLine': loc.end_line}}


def _syntax_files(parser, paths):
    from tree_sitter import Language, Parser
    grammar = importlib.import_module('tree_sitter_' + {'csharp': 'c_sharp'}.get(parser.language, parser.language))
    syntax = Parser(Language(grammar.language()))
    parser.syntax_files = {}
    parser.syntax_symbols = []
    for path in paths:
        file = path.relative_to(parser.root).as_posix()
        source = parser.sources[file] = SourceText(file, path.read_bytes(), parser.language == 'python')
        tree = syntax.parse(source.text.encode('utf-8'))
        if tree.root_node.has_error:
            raise ValueError('PARSE_ERROR：源码语法树存在错误：' + file)
        symbols = []
        root = tree.root_node
        package = next((n for n in root.named_children if n.type in {'package_declaration', 'file_scoped_namespace_declaration'}), None)
        namespace = _text(_field(package, 'name')) or re.sub(r'^(?:package|namespace)\s+|;$', '', _text(package)).strip()

        def collect(node, owner=None, scope=namespace):
            if node.type in {'namespace_declaration', 'namespace_definition'}:
                own_scope = _text(_field(node, 'name'))
                scope = '.'.join(filter(None, [scope, own_scope]))
            cpp_use = (parser.language == 'cpp' and node.type in _TYPES and _field(node, 'body') is None and
                       not (node.parent is not None and node.parent.type in {'declaration', 'field_declaration'} and _field(node.parent, 'declarator') is None))
            is_type = node.type in _TYPES and not cpp_use
            is_function = node.type in _FUNCTIONS
            prototype = node.type in {'declaration', 'field_declaration'} and any(
                n.type == 'function_declarator' for n in _walk(_field(node, 'declarator')))
            global_def = (owner is None or owner.kind == 'type') and node.type in {
                'assignment', 'variable_declarator', 'init_declarator', 'enumerator',
                'property_declaration', 'alias_declaration', 'type_definition', 'preproc_def', 'preproc_function_def'}
            name = _text(_field(node, 'name'))
            if (is_function or prototype) and not name:
                name = _text(_declarator(node))
            if global_def and not name:
                name = _text(_field(node, 'left') or _declarator(node) or _field(node, 'pattern'))
            next_owner = owner
            if (is_type or is_function or prototype or global_def) and name:
                span = node.parent if node.parent is not None and node.parent.type in {'decorated_definition', 'template_declaration'} else node
                if global_def and node.type in {'init_declarator', 'variable_declarator'} and node.parent.type in {'declaration', 'field_declaration'}:
                    span = node.parent
                qualified = '.'.join(filter(None, [scope if '::' in name else owner.qualified if owner else scope, name.replace('::', '.')]))
                item = SimpleNamespace(ast=node, span=span, owner=owner, name=name.replace('::', '.').rsplit('.', 1)[-1],
                    qualified=qualified, kind='type' if is_type else 'global' if global_def else 'function',
                    prototype=prototype, source=source, file=file)
                symbols.append(item)
                if is_type or is_function:
                    next_owner = item
            for child in node.named_children:
                collect(child, next_owner, scope)

        collect(root)
        parser.syntax_symbols.extend(symbols)
        parser.syntax_files[file] = (tree, symbols)


def _syntax_result(parser, file, raw):
    source = parser.sources[file]
    if source.raw != raw:
        raise ValueError('SOURCE_CHANGED：语法分析期间源码变化：' + file)
    tree, symbols = parser.syntax_files[file]
    root = tree.root_node
    nodes = list(_walk(root))
    key = lambda s: {'filePath': file, 'name': s.name, 'startLine': s.ast.start_point.row + 1}
    owner_at = lambda n: min((s for s in symbols if s.kind != 'global' and s.span.start_byte <= n.start_byte and
                             n.end_byte <= s.span.end_byte), key=lambda s: s.span.end_byte - s.span.start_byte, default=None)
    declarations, facts, import_rows, initial = [], [], [], []
    imports = {}
    declared_names = {s.name for s in parser.syntax_symbols}
    known = _KNOWN - declared_names

    def ancestors(node):
        while node is not None:
            yield node
            node = node.parent

    def context(node):
        result = node
        for parent in ancestors(node.parent):
            if parent.type in _FUNCTIONS | {'class_definition'}:
                break
            if parent.type == 'if_statement':
                result = parent
        return _span(source, result)

    def emit(node, name, kind, **extra):
        if not name:
            return
        owner = owner_at(node)
        row = _span(source, node)
        conditions = [_text(_field(p, 'condition')) for p in ancestors(node.parent) if p.type == 'if_statement']
        fact = {**row['range'], 'filePath': file, 'consumer': name, 'referenceKind': kind,
                'kind': 'calls' if kind.startswith('call') else 'references',
                'owner': key(owner) if owner else {'kind': 'file', 'filePath': file}, **extra}
        if owner:
            fact['ownerRange'] = _span(source, owner.span)['range']
        if conditions:
            fact['conditions'] = conditions
            fact['typeOnly'] = any(re.fullmatch(r'(?:typing\.)?TYPE_CHECKING', c) for c in conditions)
        if parser.language == 'csharp' and kind != 'import' and not kind.startswith('call'):
            generic = node if node.type == 'generic_name' else node.parent
            args = next((n for n in generic.named_children if n.type == 'type_argument_list'), None) if generic is not None else None
            fact['genericArity'] = len(args.named_children) if args is not None else 0
        facts.append(fact)

    for symbol in symbols:
        node, span, owner = symbol.ast, symbol.span, symbol.owner
        body = _field(node, 'body')
        header = source.text.encode('utf-8')[span.start_byte:body.start_byte if body is not None else node.end_byte].decode('utf-8')
        kind = ('struct' if 'struct' in node.type else 'interface' if 'interface' in node.type else 'enum' if 'enum' in node.type else 'class') if symbol.kind == 'type' else (
            'declaration' if symbol.kind == 'global' else 'constructor' if node.type == 'constructor_declaration' else
            'method' if owner and owner.kind == 'type' or parser.language == 'cpp' and '::' in _text(_field(node, 'declarator')) else 'function')
        params = _params(node)
        param_node = _field(node, 'parameters') or next((n for n in _walk(_field(node, 'declarator')) if n.type == 'parameter_list'), None)
        row = {**_span(source, span), 'key': key(symbol), 'kind': kind, 'qualifiedName': symbol.qualified,
               'container': key(owner) if owner else None, 'containerRange': _span(source, owner.span)['range'] if owner else None,
               'signature': _text(param_node) or None, 'parameters': [p[1] or p[0] for p in params],
               'requiredParameters': sum(not p[2] for p in params), 'hasBody': body is not None,
               'partial': bool(re.search(r'\bpartial\b', header)), 'internal': bool(re.search(r'\bstatic\b', header)) and owner is None,
               'eager': span.start_byte < node.start_byte or symbol.kind == 'type' and _field(node, 'superclasses') is not None}
        generic = next((n for n in node.named_children if n.type == 'type_parameter_list'), None)
        row['genericArity'] = len(generic.named_children) if generic is not None else 0
        contexts, conditions = [], []
        if parser.language == 'cpp':
            encoded = source.text.encode('utf-8')
            for parent in ancestors(node.parent):
                if parent.type == 'namespace_definition':
                    block = _field(parent, 'body')
                    if block is not None:
                        contexts.extend([_span(source, start=parent.start_byte, end=block.start_byte + 1),
                                         _span(source, start=block.end_byte - 1, end=parent.end_byte)])
                if parent.type in {'preproc_if', 'preproc_ifdef', 'preproc_else', 'preproc_elif'}:
                    line_end = encoded.find(b'\n', parent.start_byte)
                    contexts.append(_span(source, start=parent.start_byte, end=line_end + 1 if line_end >= 0 else parent.end_byte))
                    if parent.type in {'preproc_if', 'preproc_ifdef'}:
                        end = encoded.rfind(b'#endif', parent.start_byte, parent.end_byte)
                        if end >= 0:
                            contexts.append(_span(source, start=end, end=parent.end_byte))
                        guard = parent.type == 'preproc_ifdef' and parent.parent.type == 'translation_unit' and any(
                            n.type == 'preproc_def' and _text(_field(n, 'name')) == _text(_field(parent, 'name')) for n in parent.named_children)
                        if not guard:
                            conditions.append(_text(parent).splitlines()[0])
        row.update(contexts=contexts, conditions=conditions)
        declarations.append(row)

    for node in nodes:
        if node.type not in _IMPORTS:
            continue
        import_rows.append(context(node))
        if node.type in {'import_statement', 'import_from_statement'}:
            module = _field(node, 'module_name')
            base = _text(module)
            for child in node.named_children:
                if child == module:
                    continue
                original = _text(_field(child, 'name')) or _text(child)
                alias = _text(_field(child, 'alias')) or (original.rsplit('.', 1)[-1] if base else original.split('.')[0])
                name = '.'.join(filter(None, [base, original]))
                if name.startswith('.'):
                    dots = len(name) - len(name.lstrip('.'))
                    parts = list(Path(file).parent.parts)
                    name = '.'.join(parts[:max(0, len(parts) - dots + 1)] + [name[dots:]])
                imports[alias] = name
                extra = {}
                if alias == '*':
                    module = name.removesuffix('.*')
                    target_file = next((f for f in parser.syntax_files if f.rsplit('.', 1)[0].replace('/', '.').removesuffix('.__init__') == module), None)
                    if target_file is not None:
                        target_tree, target_symbols = parser.syntax_files[target_file]
                        exported = {s.name: module + '.' + s.qualified for s in target_symbols if s.owner is None and not s.name.startswith('_')}
                        assignment = next((n for n in _walk(target_tree.root_node) if n.type == 'assignment' and _text(_field(n, 'left')) == '__all__'), None)
                        value = _field(assignment, 'right')
                        if value is not None and value.type in {'list', 'tuple'} and all(re.fullmatch(r"['\"][A-Za-z_]\w*['\"]", _text(n)) for n in value.named_children):
                            names = [_text(n)[1:-1] for n in value.named_children]
                            extra['exportNames'] = [exported.get(n, module + '.' + n) for n in names]
                        else:
                            extra['exportNames'] = list(exported.values())
                emit(node, name, 'import', variable=alias, producer=name, importContext=context(node), **extra)
        elif node.type == 'preproc_include':
            path = _text(_field(node, 'path'))
            emit(node, path[1:-1], 'include', external=path.startswith('<'))
        elif node.type in {'using_directive', 'import_declaration'}:
            names = [n for n in node.named_children if n.type not in {'modifier', 'modifiers'}]
            if names:
                full = _text(names[-1])
                alias = _text(names[0]) if '=' in _text(node) else full.rsplit('.', 1)[-1]
                imports[alias] = full
                emit(node, full, 'import', variable=alias, producer=full,
                     namespace=node.type == 'using_directive' and '=' not in _text(node) and 'static' not in _text(node))

    def enclosing(node):
        return next((n for n in ancestors(node) if n.type in _FUNCTIONS | {'lambda', 'lambda_expression'}), None)

    def return_type(name):
        types = {_text(_field(s.ast, 'type')) for s in parser.syntax_symbols
                 if s.kind == 'function' and name in {s.name, s.qualified}}
        types.discard('')
        return next(iter(types)) if len(types) == 1 else ''

    scopes = [s.ast for s in symbols if s.kind == 'function' and not s.prototype] + [n for n in nodes if n.type in {'lambda', 'lambda_expression'}]
    local, hints = {}, {}
    for scope in sorted(scopes, key=lambda n: n.start_byte):
        params = _params(scope)
        names, types = {p[0] for p in params}, {p[0]: p[1] for p in params if p[1]}
        owner = next((s.owner for s in symbols if s.ast == scope), None)
        if parser.language == 'cpp' and owner is not None and owner.kind == 'type':
            body = _field(owner.ast, 'body')
            for member in body.named_children if body is not None else ():
                if member.type != 'field_declaration' or any(n.type == 'function_declarator' for n in _walk(_field(member, 'declarator'))):
                    continue
                name = _text(_declarator(member))
                if name and name not in names:
                    names.add(name)
                    types[name] = _text(_field(member, 'type'))
        for node in _walk(_field(scope, 'body')):
            if enclosing(node) != scope:
                continue
            if node.type not in {'assignment', 'assignment_expression', 'variable_declarator', 'init_declarator', 'for_statement', 'for_range_loop', 'declaration'}:
                continue
            left = _field(node, 'left') or _field(node, 'name') or _declarator(node)
            if left is None or left.type != 'identifier':
                continue
            binding = _text(left)
            names.add(binding)
            value = _field(node, 'right') or _field(node, 'value')
            hint = _text(_field(node, 'type') or _field(node.parent, 'type'))
            if hint in {'var', 'auto'}:
                hint = ''
            if value is not None and value.type == 'object_creation_expression':
                hint = _text(_field(value, 'type'))
            elif value is not None and value.type in _CALLS:
                call = _text(_field(value, 'function'))
                hint = hint or return_type(call) or (call if any(s.kind == 'type' and call in {s.name, s.qualified} for s in parser.syntax_symbols) else '')
            if hint:
                types[binding] = hint
        parent = enclosing(scope.parent)
        inherited = dict(hints.get(parent.id, {})) if parent is not None else {}
        for name in names:
            inherited.pop(name, None)
        inherited.update(types)
        hints[scope.id] = inherited
        local[scope.id] = names | (local.get(parent.id, set()) if parent is not None else set())

    def canonical(name, fn):
        name = name.replace('::', '.').replace('->', '.')
        first, sep, tail = name.partition('.')
        mapped = hints.get(fn.id, {}).get(first) if fn is not None else None
        if first not in (local.get(fn.id, set()) if fn is not None else set()):
            mapped = mapped or imports.get(first)
        value = mapped + (sep + tail if sep else '') if mapped else name
        if parser.language != 'cpp' or not mapped or not sep:
            return value
        def plain(type_name):
            while re.search(r'<[^<>]*>', type_name):
                type_name = re.sub(r'<[^<>]*>', '', type_name)
            return re.sub(r'\b(?:const|volatile|struct|class)\b|[&*]', '', type_name).strip().replace('::', '.')
        owner_type = plain(mapped)
        parts = tail.split('.')
        for part in parts[:-1]:
            owners = [s for s in parser.syntax_symbols if s.kind == 'type' and owner_type in {s.name, s.qualified}]
            if len(owners) != 1:
                return value
            body = _field(owners[0].ast, 'body')
            member = next((n for n in body.named_children if n.type == 'field_declaration' and _text(_declarator(n)) == part), None) if body is not None else None
            if member is None:
                return value
            owner_type = plain(_text(_field(member, 'type')))
        owners = [s for s in parser.syntax_symbols if s.kind == 'type' and owner_type in {s.name, s.qualified}]
        return owners[0].qualified + '.' + parts[-1] if len(owners) == 1 else value

    def arg_type(node, fn):
        if node.type in {'number_literal', 'integer', 'integer_literal', 'decimal_integer_literal'}:
            return 'double' if re.search(r'[.eEfF]', _text(node)) else 'int'
        if node.type in {'string', 'string_literal', 'raw_string_literal'}:
            return 'const char*' if parser.language == 'cpp' else 'string'
        if node.type in {'true', 'false', 'boolean_literal'}:
            return 'bool'
        if node.type == 'identifier':
            return hints.get(fn.id, {}).get(_text(node), '') if fn is not None else ''
        if node.type in _CALLS:
            return return_type(canonical(_text(_field(node, 'function') or _field(node, 'name')), fn))
        return _text(_field(node, 'type')) if node.type in {'cast_expression', 'object_creation_expression'} else ''

    used = set()
    for node in nodes:
        if any(p.type in _IMPORTS for p in ancestors(node)):
            continue
        fn = enclosing(node)
        names = local.get(fn.id, set()) if fn is not None else set()
        if node.type in _CALLS:
            callable = _field(node, 'function')
            name = _text(_field(node, 'type')) if node.type == 'object_creation_expression' else _text(_field(node, 'name') or callable)
            receiver = _field(node, 'object')
            if callable is not None and callable.type in {'attribute', 'member_access_expression', 'field_expression'}:
                receiver = _field(callable, 'object') or _field(callable, 'expression') or _field(callable, 'argument') or _field(callable, 'value')
                name = _text(_field(callable, 'attribute') or _field(callable, 'name') or _field(callable, 'field'))
            if receiver is not None:
                if _text(receiver) in {'self', 'cls', 'this', 'base', 'super'}:
                    owner = owner_at(node)
                    name = (owner.owner.qualified + '.' if owner and owner.owner else '') + name
                else:
                    name = _text(receiver) + '.' + name
            name = canonical(name, fn)
            if receiver is not None and parser.language != 'python':
                receiver_type = canonical(_text(receiver), fn)
                if any(s.kind == 'type' and receiver_type in {s.name, s.qualified} for s in parser.syntax_symbols):
                    emit(receiver, receiver_type, 'type')
            args = _field(node, 'arguments')
            args = args.named_children if args is not None else []
            if name not in known:
                dynamic = name.rsplit('.', 1)[-1] in {'eval', 'exec', 'getattr', 'setattr', '__import__', 'getMethod', 'GetMethod', 'Invoke', 'invoke', 'CreateInstance'}
                emit(node, name, 'dynamic' if dynamic else 'call/' + str(len(args)), argumentTypes=[arg_type(n, fn) for n in args])
            used.update(n.id for n in _walk(callable))
        if node.type == 'qualified_identifier' and node.id not in used:
            emit(node, canonical(_text(node), fn), 'type')
            used.update(n.id for n in _walk(node) if n.type != 'type_identifier')
        if node.type in {'preproc_if', 'preproc_ifdef'}:
            guard = node.type == 'preproc_ifdef' and node.parent.type == 'translation_unit' and any(
                n.type == 'preproc_def' and _text(_field(n, 'name')) == _text(_field(node, 'name')) for n in node.named_children)
            if not guard:
                emit(node, _text(_field(node, 'condition') or _field(node, 'name')) or 'conditional compilation', 'dynamic')
        if node.type not in {'identifier', 'type_identifier', 'field_identifier'} or node.id in used or _text(node) in known:
            continue
        parent = node.parent
        if any(s.ast == parent and _field(parent, 'name') == node for s in symbols):
            continue
        if parent is not None and (_field(parent, 'declarator') == node or _field(parent, 'left') == node or
            parent.type in {'parameter', 'formal_parameter', 'parameters', 'type_parameter'} and _field(parent, 'name') == node):
            continue
        if _text(node) in names and not (parser.language == 'cpp' and node.type == 'type_identifier'):
            continue
        if parent is not None and parent.type in {'attribute', 'member_access_expression', 'field_expression'}:
            member = _field(parent, 'attribute') or _field(parent, 'name') or _field(parent, 'field')
            if member != node:
                continue
            receiver = _field(parent, 'object') or _field(parent, 'expression') or _field(parent, 'argument')
            name = _text(receiver) + '.' + _text(node)
            if _text(receiver) in {'self', 'cls', 'this'}:
                owner = owner_at(node)
                name = (owner.owner.qualified + '.' if owner and owner.owner else '') + _text(node)
            emit(parent, canonical(name, fn), 'global')
        else:
            emit(node, canonical(_text(node), fn), 'type' if node.type == 'type_identifier' else 'global')

    if parser.language == 'python':
        def immediate(node):
            yield node
            children = _walk(_field(node, 'parameters')) if node.type == 'function_definition' else (
                c for child in node.named_children for c in immediate(child))
            yield from children
        for node in root.named_children:
            if node.type in {'comment', 'import_statement', 'import_from_statement'}:
                continue
            if node.type == 'function_definition' and not any(n.type in _CALLS for n in immediate(node)):
                continue
            if node.type == 'class_definition':
                body = _field(node, 'body')
                active = any(n.type in _CALLS for c in body.named_children if c.type not in {'function_definition', 'class_definition'} for n in immediate(c))
                bases = {_text(n) for n in _walk(_field(node, 'superclasses')) if n.type == 'identifier'}
                registration = any(s.kind == 'type' and s.name in bases and any(
                    m.owner is s and m.name == '__init_subclass__' for m in parser.syntax_symbols) for s in parser.syntax_symbols)
                if not active and not registration and 'metaclass' not in _text(_field(node, 'superclasses')):
                    continue
            if node.type == 'if_statement' and _field(node, 'alternative') is None and re.fullmatch(
                r'(?:typing\.)?TYPE_CHECKING|__name__\s*==\s*[\'"]__main__[\'"]', _text(_field(node, 'condition'))):
                continue
            if node.type == 'expression_statement' and all(n.type == 'string' for n in node.named_children):
                continue
            initial.append(_span(source, node))
    elif parser.language == 'cpp':
        for node in nodes:
            if owner_at(node) is not None:
                continue
            if node.type == 'declaration' and not re.match(r'^(extern|typedef)\b', _text(node)):
                decl = _field(node, 'declarator')
                if decl is not None and not any(n.type == 'function_declarator' for n in _walk(decl)):
                    type_node = _field(node, 'type')
                    constructed = type_node is not None and type_node.type != 'primitive_type' and decl.type not in {'pointer_declarator', 'reference_declarator'}
                    if constructed or any(n.type in _CALLS for n in _walk(_field(decl, 'value'))):
                        initial.append(_span(source, node))
    return {'declarations': declarations, 'facts': facts, 'imports': import_rows,
            'initialization': bool(initial), 'initializationContents': initial, 'warnings': []}


def external_imports(inventory, parser):
    """只将确有导入、且工程内不存在对应模块的引用确认为库依赖。"""
    result = []
    for file, parsed in inventory.files.items():
        for ref in parsed.references:
            if ref.kind not in {'import', 'include'}:
                continue
            targets, spelling = parser._resolve(ref, inventory)
            root = spelling.split('.')[0]
            internal = any(s.qualified_name == root or s.qualified_name.startswith(root + '.')
                           for s in inventory.symbols.values() if s.kind == 'file')
            if targets or internal or not root or spelling.startswith('.'):
                continue
            if ref.kind == 'include' and '<' not in parser.sources[file].fragment(ref.location).source:
                # 未找到的本地头文件属于缺失引用，不能冒充外部库
                continue
            text = next((text for loc, text in parser.import_contents.get(file, ()) if covers(loc, ref.location)), '')
            if not text:
                text = parser.sources[file].fragment(ref.location).source
            parser.external_roots.add(root)
            result.append({'name': spelling.removesuffix('.*'), 'import': text,
                           'filePath': file, 'startLine': ref.location.start_line})
    return result


def unit_ranges(unit, parser):
    locations = [fragment.location for fragment in unit.fragments]
    for file in dict.fromkeys(loc.file_path for loc in locations):
        for loc, text in parser.import_contents.get(file, ()):
            if text in unit.imports and not any(covers(existing, loc) for existing in locations):
                locations.append(loc)
    rows = []
    for loc in sorted(set(locations), key=lambda x: (x.file_path, x.start_line, x.end_line)):
        # 检查字节原文仍属于同一快照，行范围作为对外读取边界
        parser.sources[loc.file_path].fragment(loc)
        rows.append({'filePath': loc.file_path, 'name': unit.qualified_name,
                     'startLine': loc.start_line, 'endLine': loc.end_line})
    return rows


