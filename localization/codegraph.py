"""书畅现有六个HTTP接口的薄客户端；不创建或管理Node进程。"""
import json
from urllib import request, error


class GraphError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class CodeGraph:
    def __init__(self, config):
        self.base_url = config.get("base_url", "http://127.0.0.1:3000").rstrip("/")
        self.timeout = config.get("timeout_seconds", 120)
        if not self.base_url.startswith(("http://", "https://")) or type(self.timeout) not in {int, float} or self.timeout <= 0:
            raise ValueError("CodeGraph地址或超时配置不合法")

    def call(self, method, root, **parameters):
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
        if not isinstance(rows, list) or any(not isinstance(n, dict) or not {"id", "name", "filePath", "startLine"} <= n.keys() for n in rows):
            raise GraphError("GRAPH_PROTOCOL_ERROR", "函数清单缺少必要节点字段")
        return rows

    def callees(self, root, node):
        result = self.call("getCallees", root, key=self.key(node))
        if not isinstance(result, dict) or not isinstance(result.get('edges'), list) or any(not isinstance(e, dict) or not {'source','target'} <= e.keys() for e in result['edges']):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '调用结果缺少合法的edges')
        return result

    def location(self, root, node):
        result = self.call("getLocation", root, key=self.key(node))
        if not isinstance(result, dict) or not isinstance(result.get('filePath'),str) or not isinstance(result.get('node'),dict) or not (result.get('container') is None or isinstance(result.get('container'),dict)):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '所属类型结果缺少合法位置')
        return result

    def content(self, root, node=None, file=None):
        selector = {"kind": "file", "filePath": file} if file is not None else self.key(node)
        result = self.call("getContent", root, input=selector)
        if not isinstance(result, dict) or not isinstance(result.get('source'),str) or not isinstance(result.get('filePath'),str):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '源码结果缺少合法source或filePath')
        return result
