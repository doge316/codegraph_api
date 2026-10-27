"""完整源码单元、语法依赖和HTTP调用闭包。"""
import os
import hashlib
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from .models import *
from .languages.common import CALL_KINDS, TYPE_KINDS, covers, diagnostic


def extract_source(service, repo, names, result):
    from collections import deque
    from .config import route, SDK_LANGUAGES
    from .ordering import layers
    root = (repo / "source_project").resolve()
    if not root.is_relative_to(repo.resolve()) or not root.is_dir():
        raise ValueError("source_project不存在或越界")
    parser = route(service.source, "Source")
    graph = service.graph
    graph.prepare(root, SDK_LANGUAGES[service.source])
    graph_nodes = graph.functions(root)
    by_graph = {n["id"]: n for n in graph_nodes}
    translated = tuple(SimpleNamespace(id=n["id"], name=n["name"], kind=n.get("kind", "function"),
                      file_path=n["filePath"], start_line=n["startLine"], end_line=n.get("endLine"),
                      qualified_name=n.get("qualifiedName")) for n in graph_nodes)
    inventory = build_inventory(root, SDK_LANGUAGES[service.source], translated, parser)
    diagnostics = [d for d in inventory.diagnostics if d.code != "GRAPH_SYMBOL_UNRESOLVED"]
    entries = []
    unresolved = False
    for name in names:
        candidates = [s for s in inventory.symbols.values() if s.kind in CALL_KINDS and (s.name == name or s.qualified_name == name)]
        candidates.sort(key=lambda s: (s.locations[0].file_path, s.locations[0].start_byte))
        status = "unique" if len(candidates) == 1 else "ambiguous" if candidates else "not_found"
        result["resolutions"].append({"name": name, "status": status, "candidates": [
            {"name": s.name, "qualified_name": s.qualified_name, "file": s.locations[0].file_path,
             "start_line": s.locations[0].start_line, "signature": s.signature} for s in candidates]})
        if status == "unique":
            entries.append(candidates[0])
        else:
            unresolved = True
    if unresolved:
        result["status"] = "ambiguous" if any(r["status"] == "ambiguous" for r in result["resolutions"]) else "not_found"
        result["diagnostics"] = [d.to_dict() for d in diagnostics]
        return result
    # 用已知图节点的所属类型查询核对入口；不把nearest container当作完整外层类型。
    for symbol in entries:
        node_id = inventory.graph_bindings.get(symbol.symbol_id)
        if node_id:
            located = graph.location(root, by_graph[node_id])
            if located.get("filePath") != symbol.locations[0].file_path:
                diagnostics.append(diagnostic("SOURCE_CHANGED", "图位置与当前源码不一致", symbol.locations[0]))
            container = located.get("container")
            if container:
                content = graph.content(root, node=container)
                path = root / content["filePath"]
                if not path.resolve().is_relative_to(root):
                    raise ValueError("图内容路径越界")
                if content["source"] not in path.read_bytes().decode("utf-8-sig"):
                    diagnostics.append(diagnostic("SOURCE_CHANGED", "所属类型原文与当前文件不一致", symbol.locations[0]))
        else:
            diagnostics.append(diagnostic("GRAPH_SYMBOL_UNRESOLVED", "入口没有唯一图节点：" + symbol.qualified_name, symbol.locations[0]))
    pending, visited, units, raw_edges = deque(entries), set(), {}, []
    external, resources, symbol_units = [], [], {}
    reverse_bindings = {}
    for sid, gid in inventory.graph_bindings.items():
        reverse_bindings.setdefault(gid, []).append(sid)
    while pending:
        symbol = pending.popleft()
        if symbol.symbol_id in visited:
            continue
        visited.add(symbol.symbol_id)
        unit = parser.expand_unit(symbol, inventory)
        symbol_units[symbol.symbol_id] = unit.unit_id
        for member in unit.member_symbol_ids:
            symbol_units[member] = unit.unit_id
        if unit.unit_id in units:
            continue
        units[unit.unit_id] = unit
        scan = parser.scan_dependencies(unit, inventory)
        external.extend(scan.external_dependencies); resources.extend(scan.resources); diagnostics.extend(scan.diagnostics)
        for dep in scan.internal_dependencies:
            pending.append(dep.target)
            raw_edges.append((unit.unit_id, dep.target.symbol_id, dep.kind, dep.evidence, dep.origin))
        for sid in unit.member_symbol_ids:
            member = inventory.symbols[sid]
            if member.kind not in CALL_KINDS:
                continue
            if hasattr(parser, 'has_callable_body') and not parser.has_callable_body(member):
                # SDK可能把抽象声明指向所有实现，这不是抽象方法体发出的调用。
                continue
            gid = inventory.graph_bindings.get(sid)
            if gid is None:
                diagnostics.append(diagnostic("GRAPH_SYMBOL_UNRESOLVED", "完整单元成员无唯一图节点：" + member.qualified_name, member.locations[0]))
                continue
            returned = graph.callees(root, by_graph[gid])
            for edge in returned.get("edges", []):
                targets = reverse_bindings.get(edge["target"], [])
                if len(targets) != 1:
                    diagnostics.append(diagnostic("UNRESOLVED_REFERENCE", "调用目标无法唯一关联当前源码", member.locations[0]))
                    continue
                target = inventory.symbols[targets[0]]
                pending.append(target)
                raw_edges.append((unit.unit_id, target.symbol_id, "call", member.locations[0], "codegraph"))
    retained, remap = [], {}
    for unit in sorted(units.values(), key=lambda u: (-sum(f.location.end_byte-f.location.start_byte for f in u.fragments), u.qualified_name)):
        covering = next((other for other in retained if all(any(covers(b.location, a.location) for b in other.fragments) for a in unit.fragments)), None)
        remap[unit.unit_id] = covering.unit_id if covering else unit.unit_id
        if not covering:
            retained.append(unit)
    keys = {u.unit_id: u.fragments[0].location.file_path + "::" + u.qualified_name for u in retained}
    if len(set(keys.values())) != len(keys):
        keys = {u.unit_id: keys[u.unit_id] + "@" + str(u.fragments[0].location.start_line) for u in retained}
    edges = []
    for source, target_symbol, kind, evidence, origin in raw_edges:
        target = symbol_units.get(target_symbol)
        if target is None:
            diagnostics.append(diagnostic("UNRESOLVED_REFERENCE", "依赖未能扩展为完整源码单元", evidence))
            continue
        source, target = remap[source], remap[target]
        if source != target:
            row = {"source": keys[source], "target": keys[target], "kind": kind, "origin": origin,
                   "file": evidence.file_path, "start_line": evidence.start_line}
            if row not in edges:
                edges.append(row)
    result["units"] = [{"key": keys[u.unit_id], "kind": u.kind, "qualified_name": u.qualified_name,
        "fragments": [{"file": f.location.file_path, "start_line": f.location.start_line,
                       "end_line": f.location.end_line, "source": f.source} for f in u.fragments],
        "imports": list(u.imports), "reason": u.reason} for u in sorted(retained, key=lambda u: keys[u.unit_id])]
    result["dependencies"] = edges
    result["batches"] = layers(keys.values(), [(e["source"], e["target"]) for e in edges])
    for file in sorted({f.location.file_path for u in retained for f in u.fragments}):
        raw = (root / file).read_bytes()
        try:
            original = raw.decode("utf-8-sig")
        except UnicodeError:
            diagnostics.append(diagnostic("NON_UTF8_SOURCE", "按原始编码和哈希保留源码，SDK仅支持UTF-8", blocking=False))
            continue
        returned = graph.content(root, file=file)["source"]
        if returned.lstrip("\ufeff") != original:
            diagnostics.append(diagnostic("SOURCE_CHANGED", "图读取的文件内容发生变化：" + file))
    diagnostics.extend(validate_inventory(inventory))
    # 未选中实体的图绑定诊断不影响当前闭包；被选中成员已逐项检查。
    diagnostics = [d for d in diagnostics if d.code != "GRAPH_SYMBOL_UNRESOLVED" or "入口" in d.message or "完整单元" in d.message]
    result["external_dependencies"] = [x.to_dict() for x in dict.fromkeys(external)]
    result["resources"] = [x.to_dict() for x in dict.fromkeys(resources)]
    result["diagnostics"] = list({d.to_json(): d.to_dict() for d in diagnostics}.values())
    result["status"] = "error" if any(d.code == "SOURCE_CHANGED" for d in diagnostics) else "partial" if any(d.blocking for d in diagnostics) else "ready"
    return result
SKIP_DIRECTORIES = {'.git', '.codegraph', 'node_modules', '.venv', 'venv', '__pycache__'}

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
    files = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            files[relative] = route.parse_file(relative, path.read_bytes())
            diagnostics.extend(files[relative].diagnostics)
        except OSError as error:
            diagnostics.append(diagnostic('SOURCE_CHANGED', f'源码读取失败：{relative}：{error}'))
    symbols = {s.symbol_id: s for parsed in files.values() for s in parsed.symbols}
    if language == 'cpp':
        import re
        types = {s.qualified_name for s in symbols.values() if s.kind in TYPE_KINDS}
        for uid, symbol in list(symbols.items()):
            if symbol.kind not in CALL_KINDS:
                continue
            prefix, separator, member = symbol.qualified_name.rpartition('.')
            plain = re.sub(r'<[^<>]*>', '', prefix)
            qualified = plain + separator + member if prefix not in types and plain in types else symbol.qualified_name
            symbols[uid] = replace(symbol, qualified_name=qualified,
                                   signature=re.sub(r'\s+', '', symbol.signature) if symbol.signature else None)
    groups = {}
    for symbol in symbols.values():
        merge = language == 'csharp' and symbol.kind in TYPE_KINDS and (symbol.qualified_name in route.partial_names)
        merge = merge or (language == 'cpp' and symbol.kind in CALL_KINDS)
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
        if language == 'cpp':
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
        if language == 'cpp' and symbol.kind in CALL_KINDS and (not owner):
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
