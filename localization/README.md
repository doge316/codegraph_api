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

主流程调用图

```text
Localization.plan_tests(翻译对, 仓库名, LLM开关)
  └─ test_plan.plan_tests(...)
       │
       ├─ static_analysis(...)
       │    read_public(...) → parser.parse(...) → static_batches(...)
       │    读取公测并静态分批，得到批次字典、待复核用例、文件路径和约束
       │
       ├─ read_review_files(...) [开启LLM且存在待复核用例]
       │    按静态快照读取完整文件原文，供后续模型复核
       │
       ├─ llm_review(...)
       │    输入静态批次、待复核用例、文件原文和约束，返回候选批次
       │    关闭LLM、没有待复核用例或调用失败时返回静态批次
       │
       └─ validate_llm_result(...)
            检查模型输出的用例覆盖及批次约束，违规时回退静态批次
            → 返回 {批次编号: 本批新增测试用例列表}
```

## 组装功能：累计测试文件

`locator.assemble_tests(batches, test_directories, output_dir=...)` 根据接口1的批次字典，生成各轮累计测试文件

| 输入 | 说明 |
|---|---|
| `batches` | 接口1返回的批次字典，批次从1连续编号，所有用例在输入中各出现一次 |
| `test_directories` | 原公测目录的绝对路径，可传单个目录或目录列表，多个目录彼此独立 |
| `output_dir` | 输出根目录的绝对路径，指定位置须尚未存在，且与原公测目录相互独立 |

以下示例复用已创建的 `locator`，目标语言与上游服务地址取实例配置

```python
batches = locator.plan_tests("Java_to_Python", "sample", use_llm=False)
rounds = locator.assemble_tests(
    batches,
    ["D:/dataset/Java_to_Python/sample/target_project/public_tests"],
    output_dir="D:/runs/sample_rounds",
)
```

返回值为 `{批次编号: 修改后的测试文件绝对路径列表}`，每个列表包含该轮累计用例所在的测试文件

两批用例都在 `test_x.py` 中时，返回形式如下

```python
{
    1: ["D:/runs/sample_rounds/round_001/target_project/public_tests/test_x.py"],
    2: ["D:/runs/sample_rounds/round_002/target_project/public_tests/test_x.py"],
}
```

| 组装规则 | 行为 |
|---|---|
| 累计保留用例 | 第1轮保留第1批，第2轮保留第1和第2批，后续依次累计 |
| 屏蔽未来用例 | Python在未来用例的完整声明位置用空白与 `pass` 占位，其他目标语言使用空白占位，保持字节长度和换行位置 |
| 保留测试上下文 | 每轮从原公测复制helper、fixture、参数数据、辅助类型和资源，并保留目录结构 |
| 安全合批 | 共享初始化或测试间调用导致前批提前需要后批功能时，合并受影响的批次，输出按实际轮次重新编号 |
| 产物发布 | 先在暂存目录生成并核验所有轮次，再一次性发布到输出根目录，原公测用于快照一致性检查 |

输出根目录下按 `round_001/target_project/原公测相对路径`、`round_002/target_project/原公测相对路径` 保存各轮文件，辅助代码和资源位于对应轮次目录中

独立函数可以自动识别单一目标语言，也可通过 `language` 指定；默认输出到独立临时目录，目录保留供调用方使用，调用方使用完毕后负责清理

```python
from localization.assembly import assemble_tests

rounds = assemble_tests(
    batches,
    ["D:/dataset/Java_to_Python/sample/target_project/public_tests"],
    output_dir="D:/runs/standalone_rounds",
    language="Python",
    graph=locator.graph,
)
```

上游服务须能访问原公测和生成目录，语法分析用于确认累计用例及保留正文，实际编译和运行还需要配套的目标业务实现、构建配置及依赖

主流程调用图

```text
Localization.assemble_tests(批次字典, 原公测目录, output_dir=输出根目录)
  └─ assembly.assemble_tests(...)
       │
       ├─ public_bundle(...) → parser.parse(...)
       │    读取原公测快照，识别全部用例、辅助定义和共享上下文
       │
       ├─ coalesce_batches(...)
       │    检查共享初始化及测试间调用，必要时合并批次
       │
       ├─ 循环处理各批次，累加本轮应保留的用例
       │    ├─ without_future_tests(...)
       │    │    屏蔽未来用例，将累计测试及辅助资源写入本轮暂存目录
       │    │
       │    └─ verifier.parse(...)
       │         重新解析本轮文件，核对语法和累计用例清单
       │         主流程同时逐字节核对保留用例正文
       │
       └─ unchanged(...) → os.rename(...)
            核对原公测快照，发布已核验的全部轮次
            → 返回 {批次编号: 修改后的测试文件绝对路径列表}
```

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
Localization.extract_source(仓库名, 源语言, 函数名列表)
  └─ source_extract.extract_source(...)
       │
       ├─ graph.prepare(...) → graph.functions(...)
       │    根据绑定翻译对确定源工程，同步索引并取得函数和方法清单
       │
       ├─ build_inventory(...) → external_imports(...)
       │    补充语法分析，建立符号、精确源码范围、引用和外部导入信息
       │
       ├─ 匹配入口 → graph.location(...) / graph.content(...)
       │    按名称或限定名匹配函数，核对所属类和源码，合并同类入口
       │
       ├─ 循环处理尚未访问的源码单元
       │    ├─ parser.expand_unit(...) → parser.scan_dependencies(...)
       │    │    补齐完整类及跨文件片段，扫描必要上下文和项目内外依赖
       │    │
       │    └─ graph.location(...) / graph.content(...)
       │         → graph.call("getDependencies", ...) / graph.callees(...)
       │         核对上游类型及调用依赖，将新确认的项目内依赖加入队列
       │    直到没有新的源码单元需要处理
       │
       ├─ unit_ranges(...)
       │    整理并去重入口、依赖的源码位置及外部导入记录
       │
       └─ graph.content(...) → validate_inventory(...)
            核对文件原文和源码快照
            → 返回 entry_units 与 dependencies.project / dependencies.external
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
