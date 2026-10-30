"""公有输入与上游可查询测试引用；内部共用，不保存最近一次调用状态。"""
import hashlib
from pathlib import Path

from .config import public_directories, files_under, route, SDK_LANGUAGES
from .models import PublicFile, PublicBundle, TestGraphSnapshot, GraphNode, GraphCall, GraphContent, stable_id


def read_public(service, repo):
    root = (repo / 'target_project').resolve()
    if not root.is_relative_to(repo.resolve()) or not root.is_dir():
        raise ValueError('target_project不存在或越界')
    roots, files = public_directories(root), []
    parser = route(service.target, 'Test')
    for public in roots:
        for path in files_under(public):
            raw = path.read_bytes()
            role = 'test' if path.suffix in parser.extensions else 'resource'
            if path.suffix == '.py' and not (path.name.startswith('test') or path.name.endswith('_test.py')):
                role = 'helper'
            files.append(PublicFile(path.relative_to(root).as_posix(), raw, hashlib.sha256(raw).hexdigest(), role))
    fingerprint = stable_id([(f.relative_path, f.sha256) for f in files])
    return PublicBundle(root, tuple(files), fingerprint), roots, parser


def query_public(service, bundle, roots):
    nodes, calls, contents, references = [], [], [], {}
    for public in roots:
        service.graph.prepare(public, SDK_LANGUAGES[service.target])
        prefix = public.relative_to(bundle.root).as_posix()
        identity = lambda value: prefix + ':' + value
        for node in service.graph.functions(public):
            path = (public / node['filePath']).resolve()
            if not path.is_relative_to(public):
                raise ValueError('图节点路径超出公有目录')
            node_id = identity(node['id'])
            nodes.append(GraphNode(node_id, node['name'], node.get('kind', 'function'),
                         path.relative_to(bundle.root).as_posix(), node['startLine'],
                         node.get('endLine', node['startLine']), node.get('qualifiedName')))
            references[node_id] = {'projectRoot': str(public), 'key': service.graph.key(node)}
            returned = service.graph.callees(public, node)
            calls.extend(GraphCall(identity(e['source']), identity(e['target'])) for e in returned['edges'])
        for file in bundle.files:
            path = bundle.root / file.relative_path
            if file.role != 'resource' and path.is_relative_to(public):
                content = service.graph.content(public, file=path.relative_to(public).as_posix())
                if content['source'].encode('utf-8') != file.content_bytes:
                    raise ValueError('SOURCE_CHANGED：图原文与当前公有文件不一致：' + file.relative_path)
                contents.append(GraphContent(file.relative_path, content['source']))
    return TestGraphSnapshot(bundle.input_hash, tuple(nodes), tuple(calls), tuple(contents)), references


def reference_identity(reference):
    if not isinstance(reference, dict) or set(reference) != {'projectRoot', 'key'}:
        raise ValueError('测试引用必须是{projectRoot, key}')
    key = reference['key']
    if not isinstance(key, dict) or set(key) != {'name', 'filePath', 'startLine'}:
        raise ValueError('测试key必须包含name、filePath、startLine')
    if not isinstance(reference['projectRoot'], str) or not Path(reference['projectRoot']).is_absolute():
        raise ValueError('测试projectRoot须为绝对路径')
    if not isinstance(key['name'], str) or not key['name'] or not isinstance(key['filePath'], str) or not key['filePath']:
        raise ValueError('测试名称及路径不能为空')
    if type(key['startLine']) is not int or key['startLine'] < 1:
        raise ValueError('startLine须为正整数')
    path = Path(key['filePath'])
    if path.is_absolute() or '..' in path.parts or '\\' in key['filePath']:
        raise ValueError('filePath须为工程内的规范相对路径')
    return (str(Path(reference['projectRoot']).resolve()), key['name'], key['filePath'], key['startLine'])


def case_references(cases, snapshot, references):
    result, seen = {}, set()
    for case in cases:
        if case.runtime_selector in result:
            raise ValueError('TEST_SELECTOR_AMBIGUOUS：同一测试选择器对应多个定义：' + case.runtime_selector)
        candidates = [n for n in snapshot.nodes if n.file_path == case.file_path and
                      case.location.start_line <= n.start_line <= case.location.end_line and
                      n.end_line <= case.location.end_line]
        named = [n for n in candidates if n.name == case.name]
        candidates = named or candidates
        if len(candidates) != 1:
            raise ValueError('TEST_KEY_UNRESOLVED：测试无法唯一关联上游可查询节点：' + case.runtime_selector)
        ref = references[candidates[0].id]
        identity = reference_identity(ref)
        if identity in seen:
            raise ValueError('TEST_KEY_AMBIGUOUS：多个测试映射到了同一节点')
        seen.add(identity)
        result[case.runtime_selector] = ref
    return result


def unchanged(bundle):
    for file in bundle.files:
        if hashlib.sha256((bundle.root / file.relative_path).read_bytes()).hexdigest() != file.sha256:
            raise ValueError('SOURCE_CHANGED：分析过程中公有文件变化：' + file.relative_path)
