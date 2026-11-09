"""HTTP接口的薄客户端；不创建或管理Node进程。"""
import json
from urllib import request, error
from contextvars import ContextVar
from functools import wraps
from copy import deepcopy

_request_cache = ContextVar('localization_graph_cache', default=None)

def request_scope(function):
    """只复用一次公开接口调用内的图查询，结束即清空，不跨请求复用旧源码。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _request_cache.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _request_cache.reset(token)
    return wrapped



class GraphError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class CodeGraph:
    def __init__(self, config):
        if not isinstance(config, dict) or not {"base_url", "timeout_seconds"} <= config.keys():
            raise ValueError("CodeGraph配置需要base_url和timeout_seconds")
        self.base_url = config["base_url"]
        self.timeout = config["timeout_seconds"]
        if not isinstance(self.base_url, str) or not self.base_url.startswith(("http://", "https://")) or type(self.timeout) not in {int, float} or self.timeout <= 0:
            raise ValueError("CodeGraph地址或超时配置不合法")
        self.base_url = self.base_url.rstrip("/")

    def call(self, method, root, **parameters):
        cache = _request_cache.get()
        # 文件原文查询用于末尾一致性核验，必须重新读取。
        reusable = method in {'getAllFunctions', 'getCallees', 'getLocation'} or (
            method == 'getContent' and parameters.get('input', {}).get('kind') != 'file')
        key = (id(self), str(root), method, json.dumps(parameters, sort_keys=True))
        if cache is not None and reusable and key in cache:
            return deepcopy(cache[key])
        result = self._call_uncached(method, root, **parameters)
        if cache is not None and reusable:
            cache[key] = deepcopy(result)
        if cache is not None and method in {'sync', 'initialize'}:
            for old in list(cache):
                if old[:2] == key[:2]:
                    del cache[old]
        return result

    def _call_uncached(self, method, root, **parameters):
        body = {"projectRoot": str(root), **parameters}
        req = request.Request(self.base_url + "/" + method, data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {})
            except (ValueError, UnicodeError):
                detail = {}
            raise GraphError(detail.get("code", "HTTP_ERROR"), detail.get("message", "HTTP " + str(exc.code)), detail) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise GraphError("GRAPH_CONNECTION_ERROR", "CodeGraph连接失败或超时；索引超时不表示服务器操作已取消") from exc
        except (ValueError, UnicodeError) as exc:
            raise GraphError("GRAPH_PROTOCOL_ERROR", "CodeGraph未返回有效JSON") from exc

    @staticmethod
    def key(node):
        return {key: node[key] for key in ("name", "filePath", "startLine")}

    def prepare(self, root, language):
        try:
            result = self.call("sync", root)
        except GraphError as exc:
            if exc.code != "NOT_INITIALIZED":
                raise
            try:
                result = self.call("initialize", root, options={"language": ["cpp", "c"] if language == "cpp" else language})
            except GraphError as initialized:
                if initialized.code != "ALREADY_INITIALIZED":
                    raise
                result = self.call("sync", root)
        if not isinstance(result, dict) or result.get("success") is False or any(result.get(k) for k in ("filesErrored", "errors", "failedFilePaths")):
            raise GraphError("GRAPH_INDEX_ERROR", "CodeGraph索引未完整完成")

    def functions(self, root):
        rows = self.call("getAllFunctions", root)
        if not isinstance(rows, list) or any(not isinstance(n, dict) or not {"name", "filePath", "startLine"} <= n.keys() for n in rows):
            raise GraphError("GRAPH_PROTOCOL_ERROR", "函数清单缺少必要节点字段")
        resolved = []
        for row in rows:
            if 'id' not in row:
                try:
                    node = self.content(root, node=row).get('node')
                except GraphError as exc:
                    if exc.code!='AMBIGUOUS_NODE':raise
                    # 清单的语义限定为可调用节点；保留服务器给出的全部真实候选，不猜重载。
                    candidates=exc.details.get('candidates',[])
                    callable_nodes=[c for c in candidates if isinstance(c,dict) and c.get('kind') in {'function','method','constructor'}
                                    and {'id','name','filePath','startLine'}<=c.keys() and self.key(c)==self.key(row)]
                    if not callable_nodes:raise
                    resolved.extend(callable_nodes)
                    continue
                if not isinstance(node, dict) or not {'id', 'name', 'filePath', 'startLine'} <= node.keys() or self.key(node) != self.key(row):
                    raise GraphError('GRAPH_PROTOCOL_ERROR', '函数key未能恢复为一致的原生节点')
                row = node
            resolved.append(row)
        return list({n["id"]:n for n in resolved}.values())

    def callees(self, root, node):
        result = self.call("getCallees", root, key=self.key(node))
        if not isinstance(result, dict) or not isinstance(result.get('edges'), list) or any(not isinstance(e, dict) or not {'source','target'} <= e.keys() for e in result['edges']):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '调用结果缺少合法的edges')
        return result

    def location(self, root, node):
        result = self.call("getLocation", root, key=self.key(node))
        if isinstance(result, dict) and 'node' not in result and isinstance(result.get('key'), dict):
            result = {**result, 'node': self.content(root, node=result['key']).get('node')}
        if not isinstance(result, dict) or not isinstance(result.get('filePath'),str) or not isinstance(result.get('node'),dict) or not (result.get('container') is None or isinstance(result.get('container'),dict)):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '所属类型结果缺少合法位置')
        return result

    def content(self, root, node=None, file=None):
        selector = {"kind": "file", "filePath": file} if file is not None else self.key(node)
        result = self.call("getContent", root, input=selector)
        if not isinstance(result, dict) or not isinstance(result.get('source'),str) or not isinstance(result.get('filePath'),str):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '源码结果缺少合法source或filePath')
        return result
