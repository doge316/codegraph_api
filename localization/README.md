# 定位工具

提供测试分批、累计测试文件组装、源码范围与依赖定位三个功能

## 接口调用说明

### 准备与配置

使用Python 3.11及以上版本、Node.js 24，在仓库根目录安装依赖并启动上游HTTP服务

```powershell
python -m pip install -e .
npm ci
npm start
```

将 `default.yaml` 的 `codegraph.base_url` 填为实际启动的上游HTTP地址，客户端和服务需要能够访问同一份工程文件及组装输出目录

数据集按 `数据集根目录/翻译对/仓库名/source_project`、`target_project` 存放，公测放在目标工程内的 `public_test` 或 `public_tests` 目录中，可位于子目录

支持 `C#_to_Java`、`C++_to_Python`、`Java_to_C#`、`Java_to_Python`、`Python_to_C++`、`Python_to_Java`、`Python_to_Rust`

### 共享配置

在仓库根目录的 `default.yaml` 中配置数据集、翻译对和上游服务

```yaml
dataset:
  path: D:/dataset
  source_lang: Java
  target_lang: Python
codegraph:
  base_url: http://127.0.0.1:3000
  timeout_seconds: 120
localization:
  llm_enabled: false
```

主流程读取后，将配置直接传入 `Localization(config)`，实例绑定配置中的翻译对，地址、超时和LLM开关均按配置取值

### 调用三个功能

独立调用时读取同一份 `default.yaml`，示例中的仓库名 `sample` 和源函数名 `run` 应替换为实际名称

```python
from localization import Localization
from localization.config import load_config

config = load_config("default.yaml")
locator = Localization(config)

# 接口1，获取每批新增测试用例
batches = locator.plan_tests("Java_to_Python", "sample", use_llm=config["localization"]["llm_enabled"])

# 组装，使用接口1结果生成各轮累计测试文件
rounds = locator.assemble_tests(
    batches,
    ["D:/dataset/Java_to_Python/sample/target_project/public_tests"],
    output_dir="D:/runs/sample_rounds",
)

# 接口2，按源函数名查找完整所属类和依赖
source = locator.extract_source("sample", "Java", ["run"])
```

| 调用 | 输入 | 返回值 |
| --- | --- | --- |
| `plan_tests(translation_pair, repository_name, use_llm)` | 翻译对、仓库目录名、LLM开关 | `{批次编号: 本批新增测试用例列表}` |
| `assemble_tests(batches, test_directories, *, output_dir=None)` | 接口1结果、一个或多个原公测目录、输出目录 | `{批次编号: 本轮累计测试文件绝对路径列表}` |
| `extract_source(repository_name, source_language, function_names)` | 仓库目录名、源语言、源函数名或限定名的字符串列表 | 入口源码范围、项目内依赖范围、外部依赖记录 |

`plan_tests` 的结果可直接交给 `assemble_tests`，`extract_source` 由调用方提供相应源函数名

本地检查脚本默认读取 `default.yaml`，结果保存到 `dev/localization/output`

```powershell
python -B dev/localization/check_interfaces.py plan --repo sample
python -B dev/localization/check_interfaces.py assemble --repo sample
python -B dev/localization/check_interfaces.py source --repo sample --functions run
```

启用LLM时，在仓库根目录 `.env` 配置模型

```dotenv
LLM_ENDPOINT=https://your-provider.example/v1/chat/completions
LLM_MODEL=your-model
LLM_API_KEY=your-key
LLM_TIMEOUT_SECONDS=120
LLM_TEMPERATURE=0
LLM_MAX_OUTPUT_TOKENS=2048
```

以上六项为必填，`LLM_THINKING` 可选，接口1在启用LLM且存在待复核用例时调用模型，组装在 `localization.llm_enabled` 开启时逐轮调用模型

## 共享测试分析

`analyze_tests(test_directories, language, graph)` 接收一个或多个公测目录、目标语言和上游HTTP客户端，返回同一文件快照上的分析字典

| 输出项 | 内容 |
| --- | --- |
| `cases` | 用例标识、完整声明位置、被测接口及调用信息 |
| `units` | 测试、fixture、helper、参数数据、导入、初始化和作用域的原文及位置 |
| `evidence` | 代码单元之间的引用、fixture注入和生命周期关联，包含原文位置、触发阶段和确认情况 |
| `files` | 源码文件原文、编码及资源内容 |
| `layouts` | 类、模块等作用域的拼接信息 |
| `snapshot`、`diagnostics` | 输入文件快照和解析诊断 |

接口1使用用例涉及的功能和调用关系进行分批，组装沿引用关系选择当前累计用例所需代码，各自生成分批约束和组装约束

## 接口1：测试分批

输出示例

```python
{
    1: [{"filePath": "public_tests/test_service.py", "name": "test_basic", "startLine": 4}],
    2: [{"filePath": "public_tests/test_service.py", "name": "test_combined", "startLine": 8}],
}
```

路径相对于 `target_project`，批次从1连续编号，每批非空，每个用例只出现一次

```text
Localization.plan_tests(...)
  └─ test_plan.plan_tests(...)
       ├─ analyze_tests(公测目录, 目标语言, graph)
       │    读取公测并建立共享的用例、代码单元和引用证据
       │
       ├─ static_analysis(analysis)
       │    静态分批 → 批次字典、待复核用例、完整文件路径和约束
       │
       ├─ llm_review(静态批次, 待复核用例, paths, constraints, use_llm)
       │    按开关读取完整文件原文并提交模型 → 候选批次
       │
       └─ validate_batches(静态批次, LLM批次, review_cases, constraints, use_llm)
            检查静态基准及模型结果 → 最终批次字典
```

LLM输入包含静态批次、待复核用例、程序读取的完整测试源码和分批约束，关闭LLM或没有待复核用例时使用静态结果

校验覆盖、重复、先后、同批及加载约束，模型须保持非复核用例的原批次，静态基准无效时报错，模型调用或结果无效时回退静态批次

## 组装：累计测试文件

返回示例

```python
{
    1: ["D:/runs/sample_rounds/round_001/target_project/public_tests/test_service.py"],
    2: ["D:/runs/sample_rounds/round_002/target_project/public_tests/test_service.py"],
}
```

每轮从累计用例出发，递归添加必要的fixture、helper、参数数据、导入、初始化及类或模块结构，按原声明顺序拼接文件

未来用例及其专用初始化在需要的轮次加入，conftest中的fixture按用例关联加入并保留框架配置与钩子，Python导入可按本轮需要保留导入项，无法安全拆开的测试调用和不透明上下文会合批，资源按原字节复制

```text
Localization.assemble_tests(...)
  └─ assembly.assemble_tests(...)
       ├─ analyze_tests(...) → planning_context(...) / coalesce_batches(...)
       │    分析公测，合并无法安全拆分的批次
       │
       ├─ 逐批累计
       │    ├─ prepare_round(batches, batch_no, previous_files, analysis, target_layout)
       │    │    选择必要代码并拼接 → 静态文件、模型上下文和组装约束
       │    │
       │    ├─ llm_assemble(static_files, context, constraints, use_llm)
       │    │    可选模型分析 → 完整文件字典
       │    │
       │    └─ validate_and_write(static_files, candidate_files, constraints, round_dir, use_llm)
       │         校验模型结果，失败则校验静态回退 → 写入暂存目录
       │         读取实际写入的文件，作为下一轮的previous_files
       │
       └─ check_snapshot(...) → os.rename(...)
            核对原公测后一次性发布各轮文件，返回测试文件路径
```

| 小接口 | 输入 | 输出 |
| --- | --- | --- |
| `prepare_round` | 批次字典、当前轮次、上一轮实际文件、共享分析、目录布局 | `{相对路径: 完整源码}`、模型上下文、组装约束 |
| `llm_assemble` | 静态文件、模型上下文、组装约束、LLM开关 | `{相对路径: 完整源码}` |
| `validate_and_write` | 静态文件、候选文件、约束、暂存轮次目录、LLM开关 | 本轮累计测试文件路径列表 |

模型获得静态文件、上一轮实际文件、本轮新增用例、原公测源码、代码单元位置、结构化关联证据和组装约束，返回完整文件字典，由程序校验并写入

校验语法、累计用例清单、测试与辅助代码原文、作用域、必要导入、声明顺序和文件范围，模型失败时使用通过校验的静态文件，两者均无效时终止本次组装并清理暂存结果

`test_directories` 使用绝对路径，可传单个目录或彼此独立的目录列表，批次须覆盖这些目录的全部用例

`output_dir` 须为尚未存在的绝对路径，与原公测目录相互独立，省略时生成临时输出目录，由调用方使用完后清理

返回列表包含本轮累计用例所在文件，辅助文件与资源位于同轮目录，运行时将该轮公测目录覆盖到目标工程副本，使用配套业务实现、构建配置和依赖执行测试

## 接口2：源码范围与依赖

返回示例

```python
{
    "entry_units": [
        {"filePath": "Service.java", "name": "Service", "startLine": 3, "endLine": 20},
    ],
    "dependencies": {
        "project": [
            {"filePath": "Repository.java", "name": "Repository", "startLine": 1, "endLine": 12},
        ],
        "external": [],
    },
}
```

`entry_units` 为入口函数所属的完整类或独立函数，`dependencies.project` 为项目内依赖范围，`dependencies.external` 为外部库名称、原始导入语句及位置

路径相对于 `source_project`，行号从1开始且包含结束行，跨文件实现及必要导入可能对应多个片段，调用方按范围读取原文

函数名按符号名称或限定名匹配，参数错误、入口缺失、歧义及预期提取失败会记录原因并返回 `{}`，调用方须检查空结果

```text
Localization.extract_source(...)
  └─ source_extract.extract_source(...)
       ├─ graph.prepare(...) → graph.functions(...)
       │    同步索引，取得源函数与方法清单
       ├─ build_inventory(...) → external_imports(...)
       │    建立符号、源码范围、引用和导入信息
       ├─ 匹配入口 → graph.location(...) / graph.content(...)
       │    核对所属类与原文
       ├─ 循环处理待扫描单元
       │    parser.expand_unit(...) → parser.scan_dependencies(...)
       │    → graph.location(...) / graph.content(...)
       │    → graph.call("getDependencies", ...) / graph.callees(...)
       │    补齐完整类及跨文件实现，将新确认依赖加入队列
       └─ unit_ranges(...) → graph.content(...) → validate_inventory(...)
            整理源码范围，核对原文并返回结果
```

上游HTTP提供节点、归属、调用、依赖和源码查询，本地AST／Tree-sitter补充框架语义、数据流、完整声明范围及跨文件实现关联

## 文件结构

```text
localization/
├─ README.md              调用说明、返回结构和主流程
├─ __init__.py            导出 Localization
├─ api.py                 三个功能的公开入口与实例配置
├─ config.py              共享配置读取、语言路由、数据集路径与公测目录发现
├─ codegraph.py           上游HTTP客户端与请求内查询缓存
├─ test_plan.py           静态分批、安全合批及统一批次校验
├─ test_analysis.py       共享公测分析、代码单元、引用证据及快照
├─ llm.py                 复核原文读取、提示词、模型配置与调用
├─ assembly.py            加法组装、模型复核、文件校验及发布
├─ source_extract.py      入口匹配、依赖遍历与源码范围输出
├─ source_analysis.py     源码解析、完整单元、跨文件实现与依赖分析
├─ source_text.py         源码解码、位置换算、原文核验及诊断
├─ ordering.py            循环依赖合并与依赖排序
└─ models.py              用例、位置、源码单元及诊断的数据结构
```
