"""公有测试分析、辅助闭包和阶段计划；不运行测试。"""
from dataclasses import replace
from .models import *
from .languages.common import interfaces


def _code(bundle, location):
    from .languages.common import Source
    file = next(f for f in bundle.files if f.relative_path == location.file_path)
    source = Source(file, location.file_path.endswith(".py"))
    return source.raw[location.start_byte:location.end_byte].decode(source.codec)


def _record(bundle, unit, kind=None):
    loc = unit.location
    return {"key": loc.file_path + "::" + unit.container + "." + unit.name + "@" + str(loc.start_line),
            "file": loc.file_path, "name": unit.name, "start_line": loc.start_line, "end_line": loc.end_line,
            "kind": kind or getattr(unit, "kind", "test"), "source": _code(bundle, loc)}


def _contexts(bundle, selected, support):
    """只保留顶层上下文与测试类外壳，不夹带未来测试方法。"""
    import ast
    from .languages.common import Source
    files = {c.file_path for c in selected} | {s.location.file_path for s in support}
    contexts = []
    for file in bundle.files:
        if file.relative_path not in files:
            continue
        source = Source(file, file.relative_path.endswith(".py"))
        if file.relative_path.endswith(".py"):
            tree = ast.parse(source.raw)
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
                    loc = source.ast_location(node)
                    contexts.append({"file": file.relative_path, "kind": "module_context", "source": _code(bundle, loc), "start_line": loc.start_line})
            containers = {c.container for c in selected if c.file_path == file.relative_path}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and any(c.endswith("." + node.name) for c in containers):
                    loc = source.ast_location(node)
                    first = source.ast_location(node.body[0]) if node.body else loc
                    contexts.append({"file": file.relative_path, "kind": "class_header", "source": source.raw[loc.start_byte:first.start_byte].decode(source.codec), "start_line": loc.start_line})
                    for field in node.body:
                        if isinstance(field, (ast.Assign, ast.AnnAssign)):
                            at = source.ast_location(field)
                            contexts.append({"file": file.relative_path, "kind": "class_field", "source": _code(bundle, at), "start_line": at.start_line})
        else:
            from .config import route
            ext_lang = {".java": "Java", ".cs": "C#", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".h": "C++", ".hpp": "C++"}
            from pathlib import PurePosixPath
            lang = ext_lang.get(PurePosixPath(file.relative_path).suffix)
            if lang:
                parser = route(lang, "Source")
                parsed = parser.parse_file(file.relative_path, source.raw)
                for declaration in parsed.symbols:
                    if declaration.kind in {"class", "struct", "interface", "enum", "declaration"}:
                        loc = declaration.locations[0]
                        text = _code(bundle, loc)
                        if declaration.kind != "declaration":
                            text = text.split("{", 1)[0] + "{"
                        contexts.append({"file": file.relative_path, "kind": "declaration_context", "source": text, "start_line": loc.start_line})
                contexts.extend({"file": file.relative_path, "kind": "import", "source": text} for text in parsed.imports)
            else:
                # Rust保留use/extern与属性；测试模块外壳由context单元补充。
                for line in source.text.splitlines():
                    if line.strip().startswith(("use ", "extern crate ", "#![")):
                        contexts.append({"file": file.relative_path, "kind": "import", "source": line})
                from .config import route
                from .languages.common import walk
                parser = route('Rust', 'Test')
                for node in walk(parser.parser.parse(source.raw).root_node):
                    if node.type in {'const_item', 'static_item', 'type_item'}:
                        contexts.append({'file': file.relative_path, 'kind': 'module_context',
                                         'source': parser.text(source, node), 'start_line': source.raw[:node.start_byte].count(b'\n') + 1})
    return list({str(x): x for x in contexts}.values())


def _group_plan(bundle, inventory, candidate, advice):
    """完整用例按功能集合分组；批次引用组，不复制测试原文。"""
    from graphlib import CycleError, TopologicalSorter
    from .ordering import layers
    cases = {c.case_id: c for c in inventory.cases}
    requirements = {a.case_id: set(a.feature_ids) for a in candidate.assignments}
    names = {f.feature_id: f.name for f in candidate.features}
    facts = {e.evidence_id: e for e in inventory.facts}
    parent = {cid: cid for cid in cases}
    merge_reasons = []

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def merge(ids, kind, message):
        ids = sorted(set(ids) & cases.keys())
        if not ids:
            return
        for cid in ids[1:]:
            parent[find(cid)] = find(ids[0])
        merge_reasons.append((set(ids), kind, message))

    signatures = {}
    for cid in cases:
        required = requirements.get(cid, set())
        # 无业务入口的用例保留独立身份，不因空集合而彼此合并。
        signature = tuple(sorted(required)) if required else (cid,)
        signatures.setdefault(signature, []).append(cid)
    for ids in signatures.values():
        merge(ids, 'same_interfaces', '公测涉及相同业务接口集合')
    for item in candidate.atomic_constraints:
        merge(item.case_ids, 'shared_test_state', item.reason)
    for ids in advice['groups']:
        merge(ids, 'llm_semantic_candidate', 'LLM提出的局部功能归类，经引用与冲突校验')

    pending = [u for u in candidate.unresolved if u.item_id not in advice['resolved']]
    uncertainties = []
    for item in pending:
        ids = set(item.case_ids) & cases.keys()
        merge(ids, 'unresolved_region', '同一未解问题涉及的用例共同处理')
        uncertainties.append({'kind': 'unresolved_call', 'message': item.reason,
                              'case_ids': ids, 'can_ask_llm': item.can_ask_llm})
    for diagnostic in inventory.diagnostics:
        if not diagnostic.blocking:
            continue
        ids = set(diagnostic.related_ids) & cases.keys()
        loc = diagnostic.location
        if not ids and loc:
            ids = {cid for cid, case in cases.items() if case.file_path == loc.file_path and
                   loc.start_byte < case.location.end_byte and case.location.start_byte < loc.end_byte}
            owners = [u for u in inventory.support_units if u.location.file_path == loc.file_path and
                      u.location.start_byte <= loc.start_byte < u.location.end_byte]
            ids.update(cid for cid, case in cases.items() if any(u.unit_id in case.support_ids for u in owners))
            if not ids and not owners:
                ids = {cid for cid, case in cases.items() if case.file_path == loc.file_path}
        if not ids and not loc and not diagnostic.related_ids:
            files = {f.relative_path for f in bundle.files if f.relative_path in diagnostic.message}
            ids = {cid for cid, case in cases.items() if case.file_path in files} if files else set(cases)
        merge(ids, 'diagnostic_region', diagnostic.message)
        row = {'kind': diagnostic.code, 'message': diagnostic.message, 'case_ids': ids, 'can_ask_llm': False}
        if loc:
            row['location'] = {'file': loc.file_path, 'start_line': loc.start_line, 'end_line': loc.end_line}
        uncertainties.append(row)

    members = {}
    for cid in cases:
        members.setdefault(find(cid), set()).add(cid)
    features = {gid: set().union(*(requirements.get(cid, set()) for cid in ids)) for gid, ids in members.items()}
    def order(gid):
        return min((cases[cid].file_path, cases[cid].location.start_byte, cases[cid].name) for cid in members[gid])

    raw_edges = []
    def edge(before, after, kind, evidence_ids=(), before_interface=None, after_interface=None):
        if before != after:
            raw_edges.append({'before': before, 'after': after, 'kind': kind,
                              'evidence_ids': tuple(evidence_ids), 'before_interface': before_interface,
                              'after_interface': after_interface})

    # 严格包含只支持复用与增量安排，不冒充源实现调用依赖。仅保留直接包含层。
    for after in members:
        subsets = sorted((g for g in members if features[g] and features[g] < features[after]),
                         key=lambda g: (-len(features[g]), order(g)))
        direct = []
        for before in subsets:
            if not any(features[before] < features[other] for other in direct):
                direct.append(before)
                edge(before, after, 'interface_subset')

    feature_edges = [(r.before_feature, r.after_feature, r.basis, r.evidence_ids) for r in candidate.relations]
    feature_edges += [(a, b, 'llm_semantic_candidate', tuple(next((r['evidence_ids'] for r in advice.get('relation_evidence', [])
                       if r['before_feature'] == a and r['after_feature'] == b), ()))) for a, b in advice['relations']]
    for before_feature, after_feature, kind, evidence_ids in feature_edges:
        for after in members:
            if after_feature not in features[after]:
                continue
            providers = [g for g in members if g != after and before_feature in features[g] and
                         (before_feature not in features[after] or features[g] < features[after])]
            if providers:
                before = min(providers, key=lambda g: (len(features[g]), order(g)))
                edge(before, after, kind, evidence_ids, names.get(before_feature), names.get(after_feature))

    # 循环关系只能局部共同处理；不把整层无关功能吸入该组。
    while True:
        graph = {find(g): set() for g in members}
        for row in raw_edges:
            a, b = find(row['before']), find(row['after'])
            if a != b:
                graph[b].add(a)
        try:
            TopologicalSorter(graph).prepare()
            break
        except CycleError as error:
            cycle = set(error.args[1])
            merge([cid for g, ids in members.items() if find(g) in cycle for cid in ids],
                  'dependency_cycle', '组间候选关系形成循环，循环内完整用例共同处理')
    final = {}
    for cid in cases:
        final.setdefault(find(cid), set()).add(cid)
    identifiers = {gid: 'G' + str(i) for i, gid in enumerate(sorted(final, key=lambda g: min(
        (cases[c].file_path, cases[c].location.start_byte, cases[c].name) for c in final[g])), 1)}
    def evidence(ids):
        return [{'file': facts[eid].location.file_path, 'start_line': facts[eid].location.start_line,
                 'end_line': facts[eid].location.end_line, 'source': _code(bundle, facts[eid].location),
                 'origin': facts[eid].origin} for eid in dict.fromkeys(ids) if eid in facts]

    relations = []
    for row in raw_edges:
        a, b = identifiers[find(row['before'])], identifiers[find(row['after'])]
        if a == b:
            continue
        record = {'before': a, 'after': b, 'kind': row['kind'], 'certainty': 'candidate',
                  'evidence': evidence(row['evidence_ids'])}
        if row['before_interface']:
            record.update(before_interface=row['before_interface'], after_interface=row['after_interface'])
        if row['kind'] == 'interface_subset':
            record['message'] = '接口集合严格包含，建议先验证较小集合；不表示源码函数调用依赖'
        if record not in relations:
            relations.append(record)
    for i, row in enumerate(uncertainties, 1):
        ids = row.pop('case_ids')
        row.update(id='U' + str(i), case_keys=sorted(cases[c].runtime_selector for c in ids),
                   group_ids=sorted({identifiers[find(c)] for c in ids}))

    supports = {u.unit_id: u for u in inventory.support_units}
    groups = []
    for gid in identifiers:
        selected = sorted((cases[cid] for cid in final[gid]), key=lambda c: (c.file_path, c.location.start_byte))
        group_id = identifiers[gid]
        required = set().union(*(requirements.get(c.case_id, set()) for c in selected))
        case_rows = []
        for case in selected:
            row = _record(bundle, case)
            row.update(key=case.runtime_selector, interfaces=sorted({r.spelling for r in case.requirements}),
                       parameterization=case.parameterization)
            case_rows.append(row)
        needed = {uid for case in selected for uid in case.support_ids}
        todo = list(needed)
        while todo:
            unit = supports.get(todo.pop())
            if unit:
                for dep in unit.dependencies:
                    if dep not in needed:
                        needed.add(dep); todo.append(dep)
        units = sorted((supports[uid] for uid in needed if uid in supports and supports[uid].kind != 'context'),
                       key=lambda u: (u.location.file_path, u.location.start_byte))
        support_rows = []
        for unit in units:
            row = _record(bundle, unit)
            row['case_keys'] = [c.runtime_selector for c in selected if unit.unit_id in c.support_ids]
            support_rows.append(row)
        unknown = [u['id'] for u in uncertainties if group_id in u['group_ids']]
        internal = [{'before_interface': names.get(a, a), 'after_interface': names.get(b, b),
                     'kind': kind, 'certainty': 'candidate', 'evidence': evidence(refs)}
                    for a, b, kind, refs in feature_edges if a in required and b in required]
        interfaces_used = sorted(names[f] for f in required if f in names)
        groups.append({'id': group_id, 'name': ' / '.join(interfaces_used) or selected[0].name,
                       'interfaces': interfaces_used, 'cases': case_rows, 'support': support_rows,
                       'context': _contexts(bundle, selected, units),
                       'resources': [{'file': f.relative_path, 'basis': '公开目录资源保守保留'} for f in bundle.files if f.role == 'resource'],
                       'internal_relations': internal, 'uncertainty_ids': unknown,
                       'status': 'partial' if unknown else 'ready',
                       'basis': [{'kind': kind, 'message': message} for ids, kind, message in merge_reasons if ids & final[gid]]})
    by_id = {g['id']: g for g in groups}
    accumulated, batches = [], []
    for index, ids in enumerate(layers(by_id, [(r['after'], r['before']) for r in relations]), 1):
        accumulated.extend(c['key'] for gid in ids for c in by_id[gid]['cases'])
        batches.append({'index': index, 'group_ids': ids, 'cumulative_case_keys': list(accumulated),
                        'status': 'partial' if any(by_id[g]['status'] == 'partial' for g in ids) else 'ready',
                        'basis': '候选批次；同批组无已识别的前后约束，不代表已证明源码独立'})
    return {'groups': groups, 'relations': relations, 'batches': batches, 'uncertainties': uncertainties}, pending


def plan_tests(service, repo, result, use_llm):
    from pathlib import Path
    import hashlib
    from .config import public_directories, files_under, route, SDK_LANGUAGES
    from .llm import suggest
    root = (repo / "target_project").resolve()
    if not root.is_relative_to(repo.resolve()) or not root.is_dir():
        raise ValueError("target_project不存在或越界")
    roots = public_directories(root)
    result["public_roots"] = [p.relative_to(root).as_posix() for p in roots]
    parser = route(service.target, "Test")
    files, nodes, calls, contents = [], [], [], []
    for public in roots:
        for path in files_under(public):
            relative = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            code = path.suffix in parser.extensions
            role = "test" if code else "resource"
            if path.suffix == ".py" and not (path.name.startswith("test") or path.name.endswith("_test.py")):
                role = "helper"
            files.append(PublicFile(relative, raw, hashlib.sha256(raw).hexdigest(), role))
        service.graph.prepare(public, SDK_LANGUAGES[service.target])
        graph_nodes = service.graph.functions(public)
        prefix = public.relative_to(root).as_posix()
        identity = lambda n: prefix + ":" + n
        for node in graph_nodes:
            path = (public / node["filePath"]).resolve()
            if not path.is_relative_to(public):
                raise ValueError("图节点路径超出公有目录")
            nodes.append(GraphNode(identity(node["id"]), node["name"], node.get("kind", "function"), path.relative_to(root).as_posix(),
                                   node["startLine"], node.get("endLine", node["startLine"]), node.get("qualifiedName")))
            returned = service.graph.callees(public, node)
            calls.extend(GraphCall(identity(e["source"]), identity(e["target"])) for e in returned.get("edges", []))
        for file in (f for f in files if f.role != "resource" and (root / f.relative_path).is_relative_to(public)):
            content = service.graph.content(public, file=(root / file.relative_path).relative_to(public).as_posix())
            contents.append(GraphContent(file.relative_path, content["source"]))
    fingerprint = stable_id([(f.relative_path, f.sha256) for f in files])
    bundle = PublicBundle(root, tuple(files), fingerprint)
    snapshot = TestGraphSnapshot(fingerprint, tuple(nodes), tuple(calls), tuple(contents))
    inventory = analyse_tests(bundle, snapshot, parser)
    result["diagnostics"] = [d.to_dict() for d in inventory.diagnostics]
    if not inventory.cases:
        result["diagnostics"].append({"code": "NO_PUBLIC_CASES", "message": "没有识别到支持的公有测试用例", "blocking": True})
        return result
    candidate = infer_relations(inventory)
    advice = suggest(service.config, candidate, inventory, use_llm, service.llm_call)
    result["llm_used"], result["llm_usage"] = advice["called"], advice["usage"]
    result["diagnostics"].extend(advice["diagnostics"])
    grouped, pending = _group_plan(bundle, inventory, candidate, advice)
    result.update(grouped)
    for item in pending:
        result["diagnostics"].append({"code": "UNRESOLVED_TEST", "message": item.reason, "blocking": True,
                                      "cases": [c.runtime_selector for c in inventory.cases if c.case_id in item.case_ids]})
    for f in files:
        if hashlib.sha256((root / f.relative_path).read_bytes()).hexdigest() != f.sha256:
            result["diagnostics"].append({"code": "SOURCE_CHANGED", "message": f.relative_path, "blocking": True})
    result["status"] = "partial" if any(d.get("blocking", True) for d in result["diagnostics"]) else "ready"
    return result
def analyse_tests(bundle: PublicBundle, snapshot: TestGraphSnapshot, route) -> TestInventory:
    if snapshot.input_hash != bundle.input_hash:
        raise ValueError('图快照与公有输入不一致')
    parsed = route.parse(bundle)
    diagnostics = list(parsed.diagnostics) + list(snapshot.diagnostics)
    source_bytes = {f.relative_path: f.content_bytes for f in bundle.files}
    for content in snapshot.contents:
        if content.file_path not in source_bytes or content.source.encode('utf-8') != source_bytes[content.file_path]:
            diagnostics.append(Diagnostic('SOURCE_CHANGED', '图原文不符合公有快照'))
    units = {u.unit_id: u for u in parsed.support_units}
    # 无测试成员的公开辅助类型需要完整返回，不能只留下声明外壳。
    helper_types = {}
    for uid, unit in list(units.items()):
        if unit.kind == 'context' and not any(
            c.location.file_path == unit.location.file_path and
            unit.location.start_byte <= c.location.start_byte < unit.location.end_byte
            for c in parsed.cases
        ):
            units[uid] = replace(unit, kind='helper_type')
            helper_types[uid] = units[uid]
    def referenced_types(item):
        from .languages.common import Source, walk
        file = next(f for f in bundle.files if f.relative_path == item.location.file_path)
        source = Source(file, route.language == 'python')
        raw = source.raw[item.location.start_byte:item.location.end_byte]
        if route.language == 'python':
            import ast
            import textwrap
            try:
                names = {n.id for n in ast.walk(ast.parse(textwrap.dedent(raw.decode(source.codec)))) if isinstance(n, ast.Name)}
            except SyntaxError:
                names = set()
        else:
            # 使用语法标识符，字符串和注释中的同名文本不算类型引用。
            names = {route.text(source, n) for n in walk(route.parser.parse(source.raw).root_node)
                     if n.type in {'identifier', 'type_identifier'} and
                     item.location.start_byte <= n.start_byte < item.location.end_byte}
        return {uid for uid, u in units.items() if u.kind in {'helper_type', 'helper'} and u.name in names and
                u.location.file_path == item.location.file_path and uid != getattr(item, 'unit_id', None)}
    for uid, unit in list(units.items()):
        extra = referenced_types(unit)
        if unit.kind == 'helper_type':
            extra.update(m.unit_id for m in units.values() if m.kind not in {'context', 'helper_type'} and
                         m.location.file_path == unit.location.file_path and
                         unit.location.start_byte <= m.location.start_byte < unit.location.end_byte)
        units[uid] = replace(unit, dependencies=tuple(dict.fromkeys(unit.dependencies + tuple(sorted(extra)))))
    # pytest的conftest fixture可跨文件提供，autouse不一定出现在测试参数中。
    autouse = set()
    fixture_aliases = {}
    if route.language == 'python':
        import ast
        from pathlib import PurePosixPath
        for file in bundle.files:
            if not file.relative_path.endswith('.py'):
                continue
            try:
                tree = ast.parse(file.content_bytes)
            except (SyntaxError, UnicodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                found = [u for u in units.values() if u.kind == 'fixture' and u.name == node.name and u.location.file_path == file.relative_path and u.location.start_line <= node.lineno <= u.location.end_line]
                if len(found) != 1:
                    continue
                uid = found[0].unit_id
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith(('fixture','yield_fixture')):
                        for kw in decorator.keywords:
                            if kw.arg == 'autouse' and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                                autouse.add(uid)
                            if kw.arg == 'name' and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                fixture_aliases[uid] = kw.value.value
    def fixture_for(name, file):
        matches = [u for u in units.values() if u.kind == 'fixture' and fixture_aliases.get(u.unit_id,u.name) == name]
        local = [u for u in matches if u.location.file_path == file]
        if local:
            return local
        from pathlib import PurePosixPath
        parent = PurePosixPath(file).parent
        visible = [u for u in matches if PurePosixPath(u.location.file_path).name == 'conftest.py' and
                   (PurePosixPath(u.location.file_path).parent == parent or PurePosixPath(u.location.file_path).parent in parent.parents)]
        if visible:
            depth = max(len(PurePosixPath(u.location.file_path).parts) for u in visible)
            return [u for u in visible if len(PurePosixPath(u.location.file_path).parts) == depth]
        return matches
    if route.language == 'python':
        for uid, unit in list(units.items()):
            if unit.kind == 'fixture':
                extra = [m.unit_id for name in unit.parameters for m in fixture_for(name,unit.location.file_path) if m.unit_id != uid]
                units[uid] = replace(unit, dependencies=tuple(dict.fromkeys(unit.dependencies + tuple(extra))))
    scopes = {c.case_id: c for c in parsed.cases}
    scopes.update({u.unit_id: u for u in parsed.support_units if u.kind != 'context'})
    graph_binding = {}
    for node in snapshot.nodes:
        matches = [(identity, item) for identity, item in scopes.items() if item.name == node.name and item.location.file_path == node.file_path and (item.location.start_line <= node.start_line <= item.location.end_line)]
        if len(matches) == 1:
            graph_binding[node.id] = matches[0][0]
    graph_dependencies = {}
    graph_facts = []
    for edge in snapshot.calls:
        caller, target = (graph_binding.get(edge.source_id), graph_binding.get(edge.target_id))
        if caller and target in units:
            graph_dependencies.setdefault(caller, set()).add(target)
            graph_facts.append(Evidence(stable_id(edge.source_id, edge.target_id, 'support'), scopes[caller].location, 'support_call', {'scope_id': caller, 'target_support_id': target, 'position': 'caller_declaration'}, 'codegraph'))
    rows = {}
    for e in parsed.syntax_facts:
        rows.setdefault(e.fact['scope_id'], []).append(e)

    def resolve(name, file, container):
        short = name.rsplit('.', 1)[-1]
        exact = [u for u in units.values() if u.kind not in {'context', 'helper_type'} and u.container + '.' + u.name == name]
        if exact:
            return exact
        prefix = name.rpartition('.')[0]
        local = [u for u in units.values() if u.kind not in {'context', 'helper_type'} and u.name == short and (u.location.file_path == file) and (u.container == container) and (not prefix or prefix in {container, '$self', '$cls', 'self', 'cls', 'this', 'base', 'super'})]
        if not local and prefix:
            local = [u for u in units.values() if u.kind not in {'context', 'helper_type'} and u.name == short and (u.container == prefix or u.container.endswith('.' + prefix))]
        return local
    for identity, unit in list(units.items()):
        dependencies = list(unit.dependencies) + list(graph_dependencies.get(identity, ()))
        for e in rows.get(identity, []):
            matches = resolve(e.fact['name'], unit.location.file_path, unit.container)
            if len(matches) == 1:
                dependencies.append(matches[0].unit_id)
        units[identity] = replace(unit, dependencies=tuple(dict.fromkeys(dependencies)))
    all_facts = {e.evidence_id: e for e in parsed.syntax_facts + tuple(graph_facts)}
    return_types = {}
    changed = True
    while changed:
        changed = False
        for uid, u in units.items():
            types = []
            for e in rows.get(uid, []):
                if not e.fact['returned']:
                    continue
                if e.fact['construct']:
                    types.append(e.fact['name'].removesuffix('.new').removesuffix('.default'))
                else:
                    matches = resolve(e.fact['name'], u.location.file_path, u.container)
                    if len(matches) == 1 and matches[0].unit_id in return_types:
                        types.append(return_types[matches[0].unit_id])
            if len(set(types)) == 1 and return_types.get(uid) != types[0]:
                return_types[uid] = types[0]
                changed = True
    cases = []
    unresolved = []
    for case in parsed.cases:
        support = set(case.support_ids) | graph_dependencies.get(case.case_id, set()) | referenced_types(case)
        if route.language == 'python':
            from pathlib import PurePosixPath
            parent = PurePosixPath(case.file_path).parent
            support.update(uid for uid in autouse if units[uid].location.file_path == case.file_path or
                           (PurePosixPath(units[uid].location.file_path).name == 'conftest.py' and
                            (PurePosixPath(units[uid].location.file_path).parent == parent or PurePosixPath(units[uid].location.file_path).parent in parent.parents)))
        if route.language != 'python':
            support.update((u.unit_id for u in units.values() if u.kind == 'fixture' and u.container == case.container))
        for name in case.fixture_names:
            found = fixture_for(name, case.file_path)
            if len(found) == 1:
                support.add(found[0].unit_id)
            else:
                unresolved.append(UnresolvedItem(stable_id(case.case_id, name), (case.case_id,), 'fixture缺失或多义：' + name, (), False))
        todo = list(support)
        while todo:
            item = units.get(todo.pop())
            if item:
                for dep in item.dependencies:
                    if dep not in support:
                        support.add(dep); todo.append(dep)
        fixture_types = {}
        for uid in support:
            unit = units.get(uid)
            if unit and uid in return_types:
                fixture_types[unit.name] = return_types[uid]
                fixture_types[fixture_aliases.get(uid, unit.name)] = return_types[uid]
        initial_support = tuple(support)
        business = {}
        visited = set()

        def expand(scope, asserted_context=False, incoming=None):
            context_key = (scope, asserted_context, tuple(sorted((incoming or {}).items())))
            if context_key in visited:
                return
            visited.add(context_key)
            bindings = dict(fixture_types) if scope == case.case_id else dict(incoming or {})
            syntax_targets = set()
            for original in rows.get(scope, []):
                owner = case if scope == case.case_id else units[scope]
                matches = resolve(original.fact['name'], original.location.file_path, owner.container)
                if len(matches) == 1 and matches[0].unit_id in return_types:
                    for variable in original.fact.get('assigned_to', []):
                        bindings[variable] = return_types[matches[0].unit_id]
            for e in rows.get(scope, []):
                fact = dict(e.fact)
                if fact['name'].startswith('$'):
                    prefix, _, member = fact['name'][1:].partition('.')
                    if prefix in bindings:
                        fact.update(name=bindings[prefix] + ('.' + member if member else ''), dynamic=False, receiver_resolution='public_helper_or_fixture')
                matches = resolve(fact['name'], e.location.file_path, case.container if scope == case.case_id else units[scope].container)
                if not matches and fact['construct']:
                    short = fact['name'].rsplit('.', 1)[-1]
                    contexts = [u for u in units.values() if u.kind == 'context' and u.name == short and (u.location.file_path == e.location.file_path)]
                    if len(contexts) == 1:
                        context = contexts[0]
                        support.add(context.unit_id)
                        for member in units.values():
                            loc = member.location
                            outer = context.location
                            if member.kind != 'context' and loc.file_path == outer.file_path and (outer.start_byte <= loc.start_byte <= loc.end_byte <= outer.end_byte):
                                support.add(member.unit_id)
                                expand(member.unit_id)
                        continue
                if len(matches) == 1:
                    helper = matches[0]
                    support.add(helper.unit_id)
                    syntax_targets.add(helper.unit_id)
                    argument_names = fact.get('argument_names', [])
                    passed = {}
                    for position, hint in enumerate(fact.get('argument_hints', [])):
                        if hint and hint.startswith('$'):
                            hint = bindings.get(hint[1:]) if '.' not in hint else None
                        keyword = argument_names[position] if position < len(argument_names) else None
                        param = keyword or (helper.parameters[position] if position < len(helper.parameters) else None)
                        if param and hint:
                            passed[param] = hint
                    expand(helper.unit_id, fact['asserted'] or (asserted_context and fact['returned']), passed)
                    continue
                if len(matches) > 1:
                    unresolved.append(UnresolvedItem(stable_id(case.case_id, e.evidence_id), (case.case_id,), '公有helper多义：' + fact['name'], (e.evidence_id,), False))
                    continue
                fact['asserted'] = fact['asserted'] or (asserted_context and fact['returned'])
                fact['scope_id'] = case.case_id
                identity = e.evidence_id if scope == case.case_id else stable_id(e.evidence_id, case.case_id, tuple(sorted(bindings.items())), 'expanded')
                if identity in business:
                    fact['asserted'] |= business[identity].fact['asserted']
                cloned = replace(e, evidence_id=identity, fact=fact)
                business[identity] = cloned
                all_facts[identity] = cloned
            for uid in graph_dependencies.get(scope, ()):
                support.add(uid)
                if uid not in syntax_targets:
                    expand(uid)
        expand(case.case_id)
        for uid in initial_support:
            if not any((key[0] == uid for key in visited)):
                unit = units.get(uid)
                incoming = {name: fixture_types[name] for name in unit.parameters if name in fixture_types} if unit else {}
                expand(uid, incoming=incoming)
        refs = interfaces(business.values())
        for ref in refs:
            if ref.resolution == 'unknown':
                unresolved.append(UnresolvedItem(stable_id(case.case_id, ref.interface_id), (case.case_id,), '接收者或调用仍不确定：' + ref.spelling, ref.evidence_ids, True))
        if not refs:
            unresolved.append(UnresolvedItem(stable_id(case.case_id, 'no-business'), (case.case_id,), '测试没有定位到可见业务接口', (), False))
        cases.append(replace(case, requirements=refs, asserted_interfaces=tuple(dict.fromkeys((r.interface_id for r in refs if r.role == 'asserted'))), support_ids=tuple(sorted(support))))
    if not cases:
        diagnostics.append(Diagnostic('NO_TESTS', '未识别到声明级公有测试'))
    snippets = {}
    for c in cases:
        raw = source_bytes[c.file_path][c.location.start_byte:c.location.end_byte]
        snippets[c.case_id] = raw.decode('utf-8', errors='replace')
    for u in units.values():
        raw = source_bytes[u.location.file_path][u.location.start_byte:u.location.end_byte]
        snippets[u.unit_id] = raw.decode('utf-8', errors='replace')
    return TestInventory(bundle.input_hash, parsed.framework, tuple(cases), tuple(units.values()), tuple(all_facts.values()), tuple(unresolved), tuple(diagnostics), snippets)

def infer_relations(inventory: TestInventory) -> CandidatePlan:
    features = {}
    assignments = []
    relations = []
    unresolved = list(inventory.unresolved)
    facts = {e.evidence_id: e for e in inventory.facts}
    for case in inventory.cases:
        required = []
        for ref in case.requirements:
            features.setdefault(ref.interface_id, Feature(ref.interface_id, ref.spelling, (ref.interface_id,)))
            required.append(ref.interface_id)
        assignments.append(Assignment(case.case_id, tuple(dict.fromkeys(required))))
        for target in case.requirements:
            for eid in target.evidence_ids:
                evidence = facts.get(eid)
                if evidence is None:
                    continue
                if evidence.fact.get('op') == 'read':
                    continue
                origins = set(evidence.fact.get('inputs', []))
                for source in case.requirements:
                    original = facts.get(source.evidence_ids[0]) if source.evidence_ids else None
                    if original and original.fact.get('construct') and (source.spelling in origins) and (source.interface_id != target.interface_id):
                        relation = Relation(source.interface_id, target.interface_id, 'test_fact', (source.evidence_ids[0], eid))
                        if relation not in relations:
                            relations.append(relation)
        if not required and (not any((case.case_id in u.case_ids for u in unresolved))):
            unresolved.append(UnresolvedItem(stable_id(case.case_id, 'empty'), (case.case_id,), '没有可定位的业务接口', (), False))
    atomic = tuple((AtomicConstraint(d.related_ids, '显式共享测试状态，必须确认重置/顺序', True) for d in inventory.diagnostics if d.code == 'SHARED_TEST_STATE'))
    return CandidatePlan(tuple(features.values()), tuple(assignments), tuple(relations), atomic, tuple(unresolved), inventory.facts)
