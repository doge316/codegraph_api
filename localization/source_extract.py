"""按完整类／独立函数定位源码，返回入口范围与项目内外依赖。"""
from collections import deque
import logging
from types import SimpleNamespace

from .codegraph import GraphError
from .config import SDK_LANGUAGES, route
from .source_text import CALL_KINDS, covers
from .source_analysis import build_inventory, validate_inventory, external_imports, unit_ranges

logger = logging.getLogger(__name__)


def extract_source(service, repo, names):
    root = (repo / 'source_project').resolve()
    if not root.is_relative_to(repo.resolve()) or not root.is_dir():
        raise ValueError('source_project不存在或越界')
    graph = service.graph
    parser = route(service.source, 'Source', graph=graph)
    graph.prepare(root, SDK_LANGUAGES[service.source])
    parser.graph_prepared = True
    nodes = graph.functions(root)
    by_graph = {n['id']: n for n in nodes}
    translated = tuple(SimpleNamespace(id=n['id'], name=n['name'], file_path=n['filePath'],
                                     start_line=n['startLine']) for n in nodes)
    inventory = build_inventory(root, SDK_LANGUAGES[service.source], translated, parser)
    imports = external_imports(inventory, parser)
    diagnostics = list(inventory.diagnostics)
    entries = []
    for name in names:
        candidates = [s for s in inventory.symbols.values() if s.kind in CALL_KINDS and
                      name in {s.name, s.qualified_name}]
        if not candidates:
            raise ValueError('未找到入口函数：' + name)
        expanded = {parser.expand_unit(s, inventory).unit_id for s in candidates}
        if len(expanded) != 1:
            raise ValueError('入口函数存在歧义：' + name)
        symbol = candidates[0]
        entries.append(symbol)
        gid = inventory.graph_bindings.get(symbol.symbol_id)
        if gid:
            try:
                located = graph.location(root, by_graph[gid])
                if located['filePath'] != symbol.locations[0].file_path:
                    raise ValueError('SOURCE_CHANGED：入口图位置与源码不一致')
                container = located.get('container')
                if container:
                    content = graph.content(root, node=container)
                    path = (root / content['filePath']).resolve()
                    if not path.is_relative_to(root) or content['source'].lstrip('\ufeff') not in path.read_text(encoding='utf-8-sig'):
                        raise ValueError('SOURCE_CHANGED：所属类原文不一致')
            except GraphError as exc:
                if exc.code != 'AMBIGUOUS_NODE':
                    raise
                logger.warning('入口上游key有歧义，使用已核验源码单元：%s', name)
    entry_units = [parser.expand_unit(s, inventory) for s in entries]
    pending, visited, units, external = deque(entries), set(), {}, []
    reverse = {}
    for sid, gid in inventory.graph_bindings.items():
        reverse.setdefault(gid, []).append(sid)
    queried_types = set()
    while pending:
        symbol = pending.popleft()
        if symbol.symbol_id in visited:
            continue
        visited.add(symbol.symbol_id)
        unit = parser.expand_unit(symbol, inventory)
        if unit.unit_id in units:
            continue
        units[unit.unit_id] = unit
        scan = parser.scan_dependencies(unit, inventory)
        pending.extend(dep.target for dep in scan.internal_dependencies)
        external.extend(scan.external_dependencies)
        diagnostics.extend(scan.diagnostics)
        for resource in scan.resources:
            logger.info('源码资源引用：%s', resource)
        calls = [ref for ref in parser.dependency_references(unit, inventory) if ref.kind.startswith('call/')]
        references = parser.dependency_references(unit, inventory)
        for sid in unit.member_symbol_ids:
            member = inventory.symbols[sid]
            gid = inventory.graph_bindings.get(sid)
            if member.kind not in CALL_KINDS or gid is None or not parser.has_callable_body(member):
                continue
            try:
                located = graph.location(root, by_graph[gid])
                container = located.get('container')
                selector = container or by_graph[gid]
                selector_key = tuple(graph.key(selector).values())
                if selector_key not in queried_types:
                    content = graph.content(root, node=selector)
                    source = parser.sources.get(content['filePath'])
                    if source is None or content['source'].lstrip('\ufeff') not in source.text.lstrip('\ufeff'):
                        raise ValueError('SOURCE_CHANGED：上游源码单元与快照不一致')
                    queried_types.add(selector_key)
                    if container:
                        dependency_result = graph.call('getDependencies', root, level='class', input=graph.key(container))
                        if not isinstance(dependency_result, dict) or not isinstance(dependency_result.get('dependencies'), list):
                            raise GraphError('GRAPH_PROTOCOL_ERROR', '类型依赖结果缺少dependencies')
                        for key in dependency_result['dependencies']:
                            if not isinstance(key, dict) or not {'name', 'filePath', 'startLine'} <= key.keys():
                                raise GraphError('GRAPH_PROTOCOL_ERROR', '类型依赖缺少合法节点key')
                            # SDK依赖须与本单元真实引用对应，避免同名接收者、重载造成多取
                            targets = [s for s in inventory.symbols.values() if s.kind in {'class', 'struct', 'interface'} and
                                       s.name == key['name'] and any(l.file_path == key['filePath'] and
                                       l.start_line <= key['startLine'] <= l.end_line for l in s.locations)]
                            for target in targets:
                                if any(any(parser.expand_unit(s, inventory).unit_id == parser.expand_unit(target, inventory).unit_id
                                           for s in parser._resolve(ref, inventory)[0]) for ref in references):
                                    pending.append(target)
                edges = graph.callees(root, by_graph[gid])['edges']
            except GraphError as exc:
                if exc.code != 'AMBIGUOUS_NODE':
                    raise
                logger.warning('调用查询key有歧义，保留已确认依赖：%s', member.qualified_name)
                continue
            for edge in edges:
                targets = reverse.get(edge['target'], ())
                if len(targets) != 1:
                    logger.warning('调用目标无法唯一关联源码：%s', edge['target'])
                    continue
                target = inventory.symbols[targets[0]]
                matching = [r for r in calls if any(covers(loc, r.location) for loc in member.locations)
                            and r.spelling.replace('::', '.').rsplit('.', 1)[-1] == target.name]
                if not matching and target.name not in {'operator()', '__call__'}:
                    continue
                resolved = [parser._resolve(ref, inventory)[0] for ref in matching]
                if resolved and all(resolved) and not any(target.symbol_id == s.symbol_id for group in resolved for s in group):
                    continue
                pending.append(target)
    retained = []
    for unit in sorted(units.values(), key=lambda u: (-sum(f.location.end_byte - f.location.start_byte for f in u.fragments), u.qualified_name)):
        if not any(all(any(covers(b.location, a.location) for b in other.fragments) for a in unit.fragments) for other in retained):
            retained.append(unit)
    result = {'entry_units': [], 'dependencies': {'project': [], 'external': []}}
    for unit in sorted(retained, key=lambda u: (u.fragments[0].location.file_path, u.fragments[0].location.start_byte)):
        entry = any(all(any(covers(b.location, a.location) for b in unit.fragments) for a in original.fragments) for original in entry_units)
        rows = result['entry_units'] if entry else result['dependencies']['project']
        for row in unit_ranges(unit, parser):
            if row not in rows:
                rows.append(row)
    # 不同源码单元可能共享同一条导入，按原文位置而非单元名称去重
    covered = set()
    for rows in (result['entry_units'], result['dependencies']['project']):
        unique = []
        for row in rows:
            key = row['filePath'], row['startLine'], row['endLine']
            if not any(file == key[0] and start <= key[1] <= key[2] <= end for file, start, end in covered):
                covered.add(key)
                unique.append(row)
        rows[:] = unique
    selected_files = {f.location.file_path for u in retained for f in u.fragments}
    selected_imports = {text for u in retained for text in u.imports}
    confirmed = [row for row in imports if row['filePath'] in selected_files and row['import'] in selected_imports]
    result['dependencies']['external'].extend(confirmed)
    for ref in dict.fromkeys(external):
        matching = [r for r in confirmed if r['filePath'] == ref.evidence.file_path and
                    (ref.name == r['name'] or ref.name.startswith(r['name'] + '.'))]
        if matching:
            continue
        row = {'name': ref.name, 'import': '', 'filePath': ref.evidence.file_path, 'startLine': ref.evidence.start_line}
        if row not in result['dependencies']['external']:
            result['dependencies']['external'].append(row)
    for file in selected_files:
        returned = graph.content(root, file=file)['source']
        if returned.lstrip('\ufeff') != parser.sources[file].text.lstrip('\ufeff'):
            raise ValueError('SOURCE_CHANGED：上游文件原文变化：' + file)
    diagnostics.extend(validate_inventory(inventory))
    for diagnostic in dict.fromkeys(diagnostics):
        logger.warning('%s：%s', diagnostic.code, diagnostic.message)
    if any(d.code in {'SOURCE_CHANGED', 'INVALID_PATH', 'PARSE_ERROR'} for d in diagnostics):
        raise ValueError('源码快照、路径或语法检查失败，不能返回不完整提取结果')
    return result
