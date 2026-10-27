"""供ScopeTrans总编排调用的两个业务接口。"""
from copy import deepcopy
from pathlib import Path

from .config import language, pair_name, repository
from .codegraph import CodeGraph, GraphError


class Localization:
    def __init__(self, config, *, llm_call=None):
        self.config = deepcopy(config)
        data = self.config["dataset"]
        root = Path(data["path"])
        if not root.is_absolute():
            raise ValueError("dataset.path须由调用方解析为绝对路径")
        self.root = root.resolve()
        self.source, self.target = language(data["source_lang"]), language(data["target_lang"])
        self.pair = pair_name(self.source + "_to_" + self.target)
        self.graph = CodeGraph(self.config.get("codegraph", {}))
        self.llm_call = llm_call

    def plan_tests(self, translation_pair, repository_name, use_llm):
        from .test_plan import plan_tests
        result = {"status": "error", "translation_pair": self.pair, "repository_name": repository_name,
                  "public_roots": [], "groups": [], "relations": [], "batches": [], "uncertainties": [], "llm_used": False, "llm_usage": None, "diagnostics": []}
        try:
            if type(use_llm) is not bool:
                raise ValueError("use_llm必须是bool")
            if pair_name(translation_pair) != self.pair:
                raise ValueError("请求翻译对与实例绑定的翻译对不一致")
            repo = repository(self.root, self.pair, repository_name)
            return plan_tests(self, repo, result, use_llm)
        except (ValueError, TypeError, OSError, GraphError) as exc:
            result["diagnostics"].append({"code": getattr(exc, "code", "PLAN_ERROR"), "message": str(exc), "blocking": True})
            return result

    def extract_source(self, repository_name, source_language, function_names):
        from .source_extract import extract_source
        result = {"status": "error", "repository_name": repository_name, "source_language": self.source,
                  "resolutions": [], "units": [], "dependencies": [], "batches": [],
                  "external_dependencies": [], "resources": [], "diagnostics": []}
        try:
            if language(source_language) != self.source:
                raise ValueError("请求源语言与实例绑定的翻译对不一致")
            if not isinstance(function_names, list) or not function_names or any(not isinstance(n, str) or not n.strip() for n in function_names):
                raise ValueError("function_names必须是非空函数名称列表")
            repo = repository(self.root, self.pair, repository_name)
            return extract_source(self, repo, list(dict.fromkeys(function_names)), result)
        except (ValueError, TypeError, OSError, GraphError) as exc:
            result["diagnostics"].append({"code": getattr(exc, "code", "EXTRACTION_ERROR"), "message": str(exc), "blocking": True})
            return result
