"""供ScopeTrans总编排调用的测试规划、测试组装与源码提取。"""
from copy import deepcopy
from pathlib import Path

from .config import language, pair_name, repository
from .codegraph import CodeGraph, GraphError, request_scope


class Localization:
    def __init__(self, config):
        self.config = deepcopy(config)
        data = self.config["dataset"]
        # 与ScopeTrans的_get_projects保持一致：相对路径以启动目录为基准。
        self.root = Path(data["path"]).expanduser().resolve()
        self.source, self.target = language(data["source_lang"]), language(data["target_lang"])
        self.pair = pair_name(self.source + "_to_" + self.target)
        self.graph = CodeGraph(self.config.get("codegraph", {}))

    @request_scope
    def plan_tests(self, translation_pair, repository_name, use_llm):
        from .test_plan import plan_tests
        if type(use_llm) is not bool:
            raise ValueError("use_llm必须是bool")
        return plan_tests(self, translation_pair, repository_name, use_llm)

    @request_scope
    def assemble_tests(self, batches, test_directories, *, output_dir=None):
        from .assembly import assemble_tests
        return assemble_tests(batches, test_directories, output_dir=output_dir,
                              language=self.target, graph=self.graph)


    @request_scope
    def extract_source(self, repository_name, source_language, function_names):
        """返回源码位置与项目内外依赖；预期提取失败记日志并返回空字典。"""
        from .source_extract import extract_source
        import logging
        try:
            if language(source_language) != self.source:
                raise ValueError("源语言与绑定翻译对不一致")
            if not isinstance(function_names, list) or not function_names or any(not isinstance(n, str) or not n.strip() for n in function_names):
                raise ValueError("function_names须为非空函数名列表")
            repo = repository(self.root, self.pair, repository_name)
            return extract_source(self, repo, list(dict.fromkeys(function_names)))
        except (ValueError, OSError, GraphError) as exc:
            logging.getLogger(__name__).error('源码提取失败 repo=%s entries=%s [%s]：%s',
                repository_name, function_names, getattr(exc, 'code', type(exc).__name__), exc)
            return {}
