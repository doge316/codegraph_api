"""静态批次、共享上下文合批及可选模型复核。"""
import logging
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from .ordering import layers
from .test_analysis import (analyze_tests, analysis_cases, reference_identity, case_reference,
                            check_snapshot, required_units, unit_identity)

logger = logging.getLogger(__name__)


def ordered_batches(batches):
    if not isinstance(batches, dict) or not batches:
        raise ValueError('批次计划须为非空字典')
    result = {}
    for index, refs in batches.items():
        if isinstance(index, str) and index.isascii() and index.isdigit() and str(int(index)) == index:
            index = int(index)
        if type(index) is not int or index < 1 or index in result or not isinstance(refs, list) or not refs:
            raise ValueError('批次编号须为不重复正整数，每批须有用例')
        for ref in refs:
            reference_identity(ref)
        result[index] = refs
    if sorted(result) != list(range(1, len(result) + 1)):
        raise ValueError('批次编号须从1连续递增')
    return dict(sorted(result.items()))


def coalesce_batches(cases, batches, parser):
    owners = {reference_identity(ref): i for i, refs in batches.items() for ref in refs}
    first_use = {}
    for case in cases:
        for ref in case.requirements:
            for part in ref.replace('::', '.').split('.'):
                first_use[part] = min(first_use.get(part, owners[case.case_id]), owners[case.case_id])
    last, reasons = 1, []
    for name in sorted(parser.context_references & first_use.keys()):
        if first_use[name] > 1:
            last = max(last, first_use[name])
            reasons.append('保留上下文提前引用后批功能：' + name)
    for case in cases:
        for dependency in case.support_ids:
            if dependency in owners and owners[dependency] > owners[case.case_id]:
                last = max(last, owners[dependency])
                reasons.append('前批测试调用后批测试')
        if case.name in parser.context_references and owners[case.case_id] > 1:
            last = max(last, owners[case.case_id])
            reasons.append('保留上下文提前引用测试：' + case.name)
    if parser.context_opaque:
        last = max(batches)
        reasons.append('共享上下文无法安全拆分')
    if last == 1:
        return batches, []
    rows = [[ref for i, refs in batches.items() if i <= last for ref in refs]]
    rows.extend(refs for i, refs in batches.items() if i > last)
    return dict(enumerate(rows, 1)), list(dict.fromkeys(reasons))


def static_batches(parsed, parser):
    cases = {c.case_id: c for c in parsed.cases}
    required = {key: set(case.requirements) for key, case in cases.items()}
    # 使用真实用例组合键作为组代表，不生成组ID或功能ID
    parent = dict(zip(cases, cases))

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def merge(keys):
        keys = sorted(keys)
        for key in keys[1:]:
            parent[find(key)] = find(keys[0])

    signatures = {}
    for key in cases:
        if required[key]:
            signatures.setdefault(tuple(sorted(required[key])), []).append(key)
    for keys in signatures.values():
        merge(keys)
    groups = {}
    for key in cases:
        groups.setdefault(find(key), []).append(key)
    features = {g: set().union(*(required[k] for k in keys)) for g, keys in groups.items()}
    feature_edges = set()
    for calls in parser.calls.values():
        by_call = {f.get('callId'): f for f in calls if f.get('callId') is not None}
        for target in calls:
            for source in calls:
                producers = set(target.get('inputs', ())) | {target.get('producer')}
                linked = source['consumer'] in producers or any(
                    by_call.get(call) is source for call in target.get('inputCalls', ()))
                if source.get('construct') and linked and source['consumer'] != target['consumer']:
                    feature_edges.add((source['consumer'], target['consumer']))
    edges, confirmed = set(), set()
    for after in groups:
        for before in groups:
            if features[before] and features[before] < features[after]:
                edges.add((after, before))
    for first, then in feature_edges:
        for after in groups:
            if then not in features[after]:
                continue
            providers = [g for g in groups if g != after and first in features[g] and
                         (first not in features[after] or features[g] < features[after])]
            if providers:
                before = min(providers, key=lambda g: (len(features[g]), g))
                edges.add((after, before))
                confirmed.add((before, after))
    batches = {i: [case_reference(cases[key]) for group in row for key in sorted(groups[group])]
               for i, row in enumerate(layers(groups, edges), 1)}
    owners = {reference_identity(ref): i for i, refs in batches.items() for ref in refs}
    constraints = {'before': [], 'together': []}
    for before, after in sorted(confirmed):
        # 循环已由排序压缩到同批，不能再强加相互矛盾的先后约束
        if owners[before] < owners[after]:
            constraints['before'].append({'first': case_reference(cases[before]), 'then': case_reference(cases[after])})
    # 同一拓扑层中仍有直接依赖的组属于循环分量，禁止模型拆开
    for after, before in sorted(edges):
        if owners[after] == owners[before]:
            constraints['together'].append([case_reference(cases[k]) for k in groups[before] + groups[after]])
    safe, reasons = coalesce_batches(parsed.cases, batches, parser)
    if reasons:
        constraints['together'].append(safe[1])
        for reason in reasons:
            logger.warning('批次合并：%s', reason)
    safe_owners = {reference_identity(ref): i for i, refs in safe.items() for ref in refs}
    constraints['before'] = [r for r in constraints['before'] if
        safe_owners[reference_identity(r['first'])] < safe_owners[reference_identity(r['then'])]]
    return safe, constraints


def planning_context(analysis):
    """仅将无法延迟的加载引用、真实用例调用和不透明区域交给分批器。"""
    references = set()
    for unit in analysis['units']:
        if unit['eager']:
            references.update(unit['uses'])
    rows = []
    for row in analysis['cases']:
        needed = required_units(analysis, [row['case']])
        calls = [other['case'] for other in analysis['cases']
                 if other is not row and unit_identity(other['unit']) in needed]
        rows.append({'case': row['case'], 'interfaces': row['interfaces'],
                     'test_calls': calls})
    return {'references': sorted(references),
            'opaque': any(unit['kind'] == 'opaque' for unit in analysis['units']), 'cases': rows}


def static_analysis(analysis):
    """共享事实 → 静态批次、待复核用例、完整文件路径和分批约束。"""
    from pathlib import Path
    check_snapshot(analysis['snapshot'])
    cases = analysis_cases(analysis)
    context = planning_context(analysis)
    # 沿用已有分组与排序算法，加载约束来自共享分析中的实际代码关联。
    cases = [replace(case, support_ids=tuple(reference_identity(r) for r in row['test_calls']))
             for case, row in zip(cases, context['cases'])]
    parser = SimpleNamespace(calls={case.case_id: row['calls'] for case, row in zip(cases, analysis['cases'])},
                             context_references=set(context['references']), context_opaque=context['opaque'])
    batches, constraints = static_batches(SimpleNamespace(cases=cases), parser)
    batches = ordered_batches(batches)
    known = {case.case_id for case in cases}
    emitted = [reference_identity(ref) for refs in batches.values() for ref in refs]
    if len(emitted) != len(known) or set(emitted) != known:
        raise ValueError('静态批次用例遗漏、重复或标识无效')
    review = [row['case'] for row in analysis['cases'] if row['review']]
    paths = [str(Path(analysis['snapshot']['root']) / file)
             for file, row in analysis['files'].items() if 'source' in row]
    constraints['context'] = context
    constraints['snapshot'] = analysis['snapshot']
    for diagnostic in analysis['diagnostics']:
        logger.warning('%s：%s', diagnostic['code'], diagnostic['message'])
    return batches, review, paths, constraints


def _check_batches(value, cases, constraints):
    """检查批次结构、完整用例清单及静态约束，返回规范编号的批次。"""
    result = ordered_batches(value)
    expected = {reference_identity(ref) for ref in cases}
    owners = {}
    for index, refs in result.items():
        for ref in refs:
            key = reference_identity(ref)
            if key not in expected or key in owners:
                raise ValueError('批次新增或重复用例')
            owners[key] = index
    if owners.keys() != expected:
        raise ValueError('批次遗漏用例')
    for edge in constraints['before']:
        if owners[reference_identity(edge['first'])] >= owners[reference_identity(edge['then'])]:
            raise ValueError('批次违反已确认先后约束')
    for group in constraints['together']:
        if len({owners[reference_identity(ref)] for ref in group}) > 1:
            raise ValueError('批次拆分了必须同批的用例')
    if 'context' in constraints:
        context = constraints['context']
        cases = [SimpleNamespace(case_id=reference_identity(row['case']), name=row['case']['name'],
                                 requirements=row['interfaces'],
                                 support_ids=[reference_identity(ref) for ref in row['test_calls']])
                 for row in context['cases']]
        parser = SimpleNamespace(context_references=set(context['references']), context_opaque=context['opaque'])
        safe, reasons = coalesce_batches(cases, result, parser)
        if safe != result:
            raise ValueError('批次违反共享上下文约束：' + '；'.join(reasons))
    return result


def validate_batches(static_batches, llm_batches, review_cases, constraints, use_llm):
    """先检查静态基准，再按开关检查模型批次，统一返回最终批次字典。"""
    try:
        baseline = ordered_batches(static_batches)
        cases = ([row['case'] for row in constraints['context']['cases']] if 'context' in constraints
                 else [ref for refs in baseline.values() for ref in refs])
        baseline = _check_batches(baseline, cases, constraints)
        if 'snapshot' in constraints:
            check_snapshot(constraints['snapshot'])
    except (ValueError, OSError) as exc:
        logger.error('静态批次未通过校验：%s', exc)
        raise
    if not use_llm or not review_cases:
        return deepcopy(baseline)
    try:
        candidate = _check_batches(llm_batches, cases, constraints)
        original = {reference_identity(ref): index for index, refs in baseline.items() for ref in refs}
        review = {reference_identity(ref) for ref in review_cases}
        owners = {reference_identity(ref): index for index, refs in candidate.items() for ref in refs}
        if any(owners[key] != index for key, index in original.items() if key not in review):
            raise ValueError('模型移动了非复核用例')
        return deepcopy(candidate)
    except ValueError as exc:
        logger.warning('LLM回复未通过校验，保留静态结果：%s', exc)
        return deepcopy(baseline)


def plan_tests(service, translation_pair, repository_name, use_llm):
    """共享测试分析 → 静态分批 → 可选模型复核 → 统一批次校验。"""
    from .config import pair_name, repository, public_directories
    from .llm import llm_review
    if pair_name(translation_pair) != service.pair:
        raise ValueError('请求翻译对与实例绑定的翻译对不一致')
    repo = repository(service.root, service.pair, repository_name)
    target = (repo / 'target_project').resolve(strict=True)
    if not target.is_relative_to(repo):
        raise ValueError('target_project路径越界')
    analysis = analyze_tests(public_directories(target), service.target, service.graph)
    batches, review, paths, constraints = static_analysis(analysis)
    candidate = llm_review(batches, review, paths, constraints, use_llm)
    return validate_batches(batches, candidate, review, constraints, use_llm)
