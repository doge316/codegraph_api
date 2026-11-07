# 定位工具

## 接口1：测试批次

`locator.plan_tests(翻译对, 仓库名, LLM开关)` 返回 `{批次编号: 本批新增用例列表}`

```python
{1: [{"filePath": "public_tests/test_x.py", "name": "test_x", "startLine": 10}]}
```

路径相对于当前仓库的 `target_project`，批次从 1 连续编号，用例只出现一次

业务实现集中于 `test_analysis.py`、`test_plan.py`、`llm.py`，内部使用用例三个字段的组合键，不生成组、问题或证据 ID

主流程仅组织以下三个主要调用，源码读取是第2步前的准备工作

| 调用 | 输入 | 输出 |
|---|---|---|
| `static_analysis` | 翻译对、仓库名，复用当前实例上下文 | 静态批次、待复核用例、相关文件绝对路径列表、约束字典 |
| `llm_review` | 静态批次、待复核用例、完整文件原文、约束、LLM开关 | 模型批次，跳过或调用／格式失败时返回静态批次 |
| `validate_llm_result` | 候选批次、静态批次、待复核用例、约束、LLM开关 | 校验通过的批次，跳过或模型结果违规时返回静态批次 |

静态分析内部负责覆盖、安全合批和快照检查，不经过模型结果校验；复核列表不受开关影响，关闭模型或没有待复核用例时，不读取复核原文、不调用模型、不执行模型结果校验

约束包含 `before`、`together` 和用于重现共享初始化检查的 `context`；`snapshot` 仅用于程序核对原文及文件清单，不发送给模型，不增加公开输出字段，也不依赖最近一次调用的隐藏状态

模型结果违规时回退静态批次；原文件变化导致静态基准失效时仍明确报错，不将过期静态结果作为可用回退

LLM 输入为完整静态批次、待复核用例列表、相关测试文件原文及约束；不提供文件读取工具，仅在开关开启且列表非空时调用，回复必须是同格式完整批次字典

程序检查覆盖、重复、固定用例位置及动态限制，调用或校验失败记录日志并保留静态结果

## 累计测试组装

```python
from localization.assembly import assemble_tests
rounds = assemble_tests(batches, ["/absolute/target_project/public_tests"],
                        output_dir="/absolute/new/rounds")
# 或复用实例配置的目标语言与上游客户端
rounds = locator.assemble_tests(batches, public_directories, output_dir="/absolute/new/rounds")
```

输出为 `{批次编号: 修改后的测试文件路径列表}`，每轮包含之前和当前的累计用例，保留辅助代码、资源和目录结构，屏蔽未来用例，不生成 manifest

独立函数可以自动识别单一目标语言，也可通过 `language` 指定；默认输出到独立临时目录，目录保留供调用方使用，调用方使用完毕后负责清理

默认保留全部辅助上下文，仅对会提前执行的共享初始化或自动 fixture 等情况必要合批，原测试目录不被修改

## 接口2：源码范围与依赖

`locator.extract_source(仓库名, 源语言, 函数名列表)` 返回

```python
{
    "entry_units": [{"filePath": "service.py", "name": "service.Service", "startLine": 5, "endLine": 30}],
    "dependencies": {
        "project": [],
        "external": [{"name": "requests", "import": "import requests", "filePath": "service.py", "startLine": 1}]
    }
}
```

位置相对于 `source_project`，行号从 1 开始且包含结束行；默认保留完整所属类或独立函数，跨文件实现及必要导入上下文使用多个片段，不将结果列表直接视作翻译顺序

外部库不附实现；无明确导入语句的外部引用使用空 `import`，不编造导入；未解析引用只记录诊断，不直接归为外部库

业务实现集中于 `source_extract.py`（主流程）和 `source_analysis.py`（补充分析）；参数错误、缺失、歧义和预期提取失败记录日志并返回 `{}`，调用方须检查空结果，程序错误继续抛出

接口2主流程使用 `initialize` / `sync`、`getAllFunctions`、`getLocation`、`getContent`、`getDependencies`、`getCallees`，不调用 `getSourceUnit` 或 `getTestAnalysis`

```text
extract_source(...)：源码提取主流程
  │
  ├─ graph.prepare(...) / graph.functions(...)
  │    同步索引，取得函数和方法清单
  │
  ├─ build_inventory(...)
  │    本地Tree-sitter补充语法分析，建立符号、源码范围及引用信息
  │
  ├─ graph.location(...) / graph.content(...)
  │    匹配输入入口，核对所属类或独立函数的源码
  │
  ├─ 循环处理尚未访问的源码单元
  │    → expand_unit(...) 补全完整类、跨文件片段和类外实现
  │    → scan_dependencies(...) 判断必要上下文及项目内外依赖
  │    → getDependencies / getCallees 查询并核对上游依赖，将新依赖入队
  │    直到没有新的源码单元需要处理
  │
  └─ unit_ranges(...) / graph.content(...) / validate_inventory(...)
       去重、核对原文快照，返回入口范围和依赖信息
```

语法补充使用项目已有的Tree-sitter依赖；只对源工程执行，接口1使用基础HTTP查询，并在本地补充测试框架和数据流信息


## 配置与功能检查

实例绑定配置中的翻译对，数据集根目录取 `dataset.path`，上游地址取 `codegraph.base_url`，模型默认读取根目录 `.env`，也可以通过 `Localization(config, llm_call=...)` 注入已有适配器

两个接口及组装仅使用基础HTTP查询，不调用 `getTestAnalysis` 或 `getSourceUnit`；基础查询未提供的测试框架、隐式依赖、数据流和完整声明范围由本地Tree-sitter补充

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider dev/localization/tests/current
```

该检查仅创建合成代码片段并使用隔离的上游服务，模型响应由桩提供，不读取数据集仓库、不调用真实 LLM

## 独立包配置

```python
from localization import Localization

locator = Localization({
    "dataset": {"path": "D:/dataset", "source_lang": "Java", "target_lang": "Python"},
    "codegraph": {"base_url": "http://127.0.0.1:3000", "timeout_seconds": 120},
})
batches = locator.plan_tests("Java_to_Python", "sample", use_llm=False)
rounds = locator.assemble_tests(batches, ["D:/dataset/Java_to_Python/sample/target_project/public_tests"],
                                output_dir="D:/runs/sample")
source = locator.extract_source("sample", "Java", ["run"])
```

Python客户端与CodeGraph服务需要访问同一份工程文件；`projectRoot` 使用服务所在机器可访问的绝对路径

`.env` 沿用 `LLM_ENDPOINT`、`LLM_MODEL`、`LLM_API_KEY`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_OUTPUT_TOKENS`、`LLM_TEMPERATURE` 和可选 `LLM_THINKING`；程序按需读取，不在导入、关闭LLM或使用注入适配器时读取密钥

接口1和组装通过 `getAllFunctions`、`getLocation`、`getCallees` 和 `getContent` 复用原生节点、归属、调用与原文；本地补充fixture、参数化、生命周期、对象传递及共享初始化信息，接口2另外使用 `getDependencies` 查询类级依赖
