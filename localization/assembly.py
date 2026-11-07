"""从批次用例和原公测目录生成累计测试文件副本。"""
import hashlib
import os
from pathlib import Path
import tempfile

from .config import SDK_LANGUAGES
from .models import PublicBundle, PublicFile
from .test_analysis import UpstreamTests, public_bundle, reference_identity, unchanged, without_future_tests
from .test_plan import ordered_batches, coalesce_batches


def assemble_tests(batches, test_directories, *, output_dir=None, language=None, graph=None, target_root=None):
    batches = ordered_batches(batches)
    directories = [test_directories] if isinstance(test_directories, (str, Path)) else test_directories
    if not isinstance(directories, (list, tuple)) or not directories:
        raise ValueError('须提供一个或多个原测试目录')
    directories = list(dict.fromkeys(Path(p).resolve(strict=True) for p in directories))
    if any(not p.is_dir() for p in directories):
        raise ValueError('测试目录不存在')
    if any(a != b and a.is_relative_to(b) for a in directories for b in directories):
        raise ValueError('测试目录不能相互嵌套')
    root = Path(target_root).resolve(strict=True) if target_root else Path(os.path.commonpath(directories))
    if target_root is None:
        if root in directories:
            root = root.parent
        root = next((p for p in [root, *root.parents] if p.name == 'target_project'), root)
    if language is None:
        from .test_analysis import EXTENSIONS
        suffixes = {p.suffix for directory in directories for p in directory.rglob('*') if p.is_file()}
        matches = [name for name, sdk in SDK_LANGUAGES.items() if suffixes.intersection(EXTENSIONS[sdk])]
        if len(matches) != 1:
            raise ValueError('不能唯一确定测试语言，请指定language')
        language = matches[0]
    parser = UpstreamTests(SDK_LANGUAGES[language], graph)
    parser.public_roots = directories
    bundle = public_bundle(root, directories, parser.extensions)
    parsed = parser.parse(bundle)
    if not parsed.cases or any(d.code == 'PARSE_ERROR' for d in parsed.diagnostics):
        raise ValueError('公测为空或无法完整解析')
    known = {c.case_id: c for c in parsed.cases}
    selected = [reference_identity(ref) for refs in batches.values() for ref in refs]
    if len(set(selected)) != len(selected) or set(selected) != known.keys():
        raise ValueError('批次用例遗漏、重复或不属于当前测试目录')
    batches, reasons = coalesce_batches(parsed.cases, batches, parser)
    if reasons:
        import logging
        logging.getLogger(__name__).warning('组装前合并不安全批次：%s', reasons)
    if output_dir is None:
        # 输出供调用方后续使用，仅清理发布前的暂存文件
        output = Path(tempfile.gettempdir()) / ('scopetrans-rounds-' + os.urandom(8).hex())
    else:
        output = Path(output_dir)
        if not output.is_absolute():
            raise ValueError('输出目录须为绝对路径')
        output = output.resolve()
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in directories):
        raise ValueError('输出目录不能覆盖或位于原测试目录内')
    if output.exists() or output.is_symlink():
        raise FileExistsError('不覆盖已有输出目录：' + str(output))
    unchanged(bundle, directories)
    output.parent.mkdir(parents=True, exist_ok=True)
    result, cumulative = {}, set()
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        staging.mkdir()
        originals = {f.relative_path: f.content_bytes for f in bundle.files}
        for index, refs in batches.items():
            cumulative.update(reference_identity(ref) for ref in refs)
            target = staging / f'round_{index:03d}' / 'target_project'
            generated = []
            for file in bundle.files:
                future = [c for c in parsed.cases if c.file_path == file.relative_path and c.case_id not in cumulative]
                raw = without_future_tests(file, future, language)
                dest = target / file.relative_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                generated.append(PublicFile(file.relative_path, raw, hashlib.sha256(raw).hexdigest(), file.role))
            verifier = UpstreamTests(SDK_LANGUAGES[language], graph)
            verifier.public_roots = [target / directory.relative_to(bundle.root) for directory in directories]
            verified = verifier.parse(PublicBundle(target, tuple(generated), 'generated'))
            if any(d.code == 'PARSE_ERROR' for d in verified.diagnostics):
                raise ValueError('组装后的测试存在语法错误')
            if {c.runtime_selector for c in verified.cases} != {known[k].runtime_selector for k in cumulative}:
                raise ValueError('组装后存在未来用例或遗漏累计用例')
            written = {f.relative_path: f.content_bytes for f in generated}
            for key in cumulative:
                loc = known[key].location
                if originals[loc.file_path][loc.start_byte:loc.end_byte] != written[loc.file_path][loc.start_byte:loc.end_byte]:
                    raise ValueError('组装改变了保留用例的正文')
            names = sorted({known[k].file_path for k in cumulative})
            result[index] = [str(output / f'round_{index:03d}' / 'target_project' / name) for name in names]
        unchanged(bundle, directories)
        os.rename(staging, output)
    return result
