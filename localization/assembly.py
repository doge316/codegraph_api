"""将简化批次组装成累计公有测试覆盖层；不复制或执行业务代码。"""
import ast
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .models import PublicBundle, PublicFile, stable_id
from .languages.common import Source
from .test_inputs import read_public, query_public, case_references, reference_identity, unchanged


def _ordered_batches(batches):
    if not isinstance(batches, dict) or not batches:
        raise ValueError('批次计划须为非空字典')
    normalized = {}
    for index, refs in batches.items():
        if isinstance(index, str) and index.isascii() and index.isdigit() and str(int(index)) == index:
            index = int(index)
        if type(index) is not int or index < 1 or index in normalized:
            raise ValueError('批次编号须为不重复的正整数')
        if not isinstance(refs, list) or not refs:
            raise ValueError('每批须包含至少一个测试引用')
        normalized[index] = refs
    if sorted(normalized) != list(range(1, len(normalized) + 1)):
        raise ValueError('批次编号须从1连续排列')
    return dict(sorted(normalized.items()))


def _without_future_tests(file, future, language):
    raw = bytearray(file.content_bytes)
    source = Source(file, language == 'Python')
    definitions = {}
    if language == 'Python':
        for node in ast.walk(ast.parse(source.raw)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                loc = source.ast_location(node)
                definitions[loc.start_byte, loc.end_byte] = node
    for case in future:
        loc = case.location
        # 保留行号及换行，后轮原文不会在前轮被收集或编译。
        raw[loc.start_byte:loc.end_byte] = bytes(b if b in (10, 13) else 32 for b in raw[loc.start_byte:loc.end_byte])
        if language == 'Python':
            node = definitions.get((loc.start_byte, loc.end_byte))
            if node is None:
                raise ValueError('测试声明位置与当前语法树不一致：' + case.runtime_selector)
            start = source.position(node.lineno, node.col_offset)
            raw[start:start + 4] = b'pass'
    if language == 'Python':
        ast.parse(bytes(raw))
    return bytes(raw)


def _check_test_calls(parsed, selected):
    """测试被当作helper调用时，不能把它删除后仍称作可执行组装。"""
    by_name = {}
    for case in parsed.cases:
        by_name.setdefault(case.name, []).append(case)
    for evidence in parsed.syntax_facts:
        scope = evidence.fact.get('scope_id')
        if scope not in selected or evidence.kind != 'call':
            continue
        name = evidence.fact.get('name', '').rsplit('.', 1)[-1]
        targets = by_name.get(name, [])
        if len(targets) == 1 and targets[0].case_id not in selected:
            raise ValueError('前轮测试调用了尚未纳入的测试，请将它们安排为同一批：' + name)


def assemble_tests(service, repo, batches, output_dir):
    batches = _ordered_batches(batches)
    output = Path(output_dir)
    if not output.is_absolute():
        raise ValueError('output_dir须为绝对路径')
    output = output.resolve()
    if output.is_relative_to(service.root) or service.root.is_relative_to(output):
        raise ValueError('输出目录不能位于数据集内或覆盖数据集父目录')
    if output.exists() or output.is_symlink():
        raise FileExistsError('不覆盖已有输出目录：' + str(output))
    bundle, roots, parser = read_public(service, repo)
    parsed = parser.parse(bundle)
    if not parsed.cases or any(d.code == 'PARSE_ERROR' for d in parsed.diagnostics):
        raise ValueError('公有测试为空或无法完整解析，不能组装')
    snapshot, references = query_public(service, bundle, roots)
    mapped = case_references(parsed.cases, snapshot, references)
    known = {reference_identity(mapped[c.runtime_selector]): c for c in parsed.cases}
    seen, resolved = set(), {}
    for index, refs in batches.items():
        resolved[index] = []
        for ref in refs:
            identity = reference_identity(ref)
            if identity not in known:
                raise ValueError('测试引用不属于当前仓库公有用例或位置已变化：' + str(ref))
            if identity in seen:
                raise ValueError('测试引用重复出现在批次计划中')
            seen.add(identity)
            resolved[index].append(known[identity])
    if seen != known.keys():
        raise ValueError('批次计划遗漏了当前公有测试用例')
    unchanged(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    result, cumulative, cumulative_refs = {}, set(), []
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        staging.mkdir()
        for index, cases in resolved.items():
            cumulative.update(c.case_id for c in cases)
            cumulative_refs.extend(mapped[c.runtime_selector] for c in cases)
            _check_test_calls(parsed, cumulative)
            round_dir = staging / f'round_{index:03d}'
            target = round_dir / 'target_project'
            target.mkdir(parents=True)
            generated = []
            for file in bundle.files:
                future = [c for c in parsed.cases if c.file_path == file.relative_path and c.case_id not in cumulative]
                raw = _without_future_tests(file, future, service.target) if future else file.content_bytes
                dest = target / file.relative_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                generated.append(PublicFile(file.relative_path, raw, hashlib.sha256(raw).hexdigest(), file.role))
            verified = parser.parse(PublicBundle(target, tuple(generated), stable_id([(f.relative_path, f.sha256) for f in generated])))
            if any(d.code == 'PARSE_ERROR' for d in verified.diagnostics):
                raise ValueError('生成测试存在语法错误')
            expected = {c.runtime_selector for c in parsed.cases if c.case_id in cumulative}
            if {c.runtime_selector for c in verified.cases} != expected:
                raise ValueError('生成文件包含未来用例或遗漏累计用例')
            originals = {f.relative_path: f.content_bytes for f in bundle.files}
            written = {f.relative_path: f.content_bytes for f in generated}
            for case in parsed.cases:
                if case.case_id in cumulative:
                    loc = case.location
                    if originals[case.file_path][loc.start_byte:loc.end_byte] != written[case.file_path][loc.start_byte:loc.end_byte]:
                        raise ValueError('组装改变了已保留测试的正文：' + case.runtime_selector)
            manifest = {'batch': index, 'new_cases': [mapped[c.runtime_selector] for c in cases],
                        'cumulative_cases': list(cumulative_refs),
                        'public_roots': [p.relative_to(bundle.root).as_posix() for p in roots]}
            (round_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            result[index] = str(output / f'round_{index:03d}' / 'target_project')
        unchanged(bundle)
        # Windows扫描器可能短暂占用刚写入的文件；失败时仍不覆盖已有输出。
        for attempt in range(5):
            try:
                os.rename(staging, output)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    return result
