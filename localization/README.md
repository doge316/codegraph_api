# ScopeTrans定位接口

测试规划和源码提取共用书畅的CodeGraph HTTP服务。测试规划返回“批次→可查询测试引用”；测试组装按批生成累计公有测试文件；源码提取返回可直接交给翻译模型的源码上下文。测试组装显式写入指定输出目录，其余业务调用不自动保存结果。

## 环境

Python 3.11+；CodeGraph服务使用根项目要求的Node.js 24.x。Python模块与服务须能访问相同的工程绝对路径。

在仓库根目录执行PowerShell命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

CodeGraph服务由总架构启动；本地可按根README安装Node依赖并另开终端：

```powershell
npm ci
npm start
```

## 总编排调用

```python
from localization import Localization

# 接入时直接传入总编排已解析的配置，不要求模块自行加载YAML。
config = {
    "dataset": {
        "path": "D:/dataset",     # 服务器上改成服务器的数据集绝对路径
        "source_lang": "Java",
        "target_lang": "Python",
    },
    "codegraph": {"base_url": "http://127.0.0.1:3000", "timeout_seconds": 120},
    "localization": {"llm_enabled": False},
}

locator = Localization(config)
plan = locator.plan_tests(
    "Java_to_Python", "microsoft_vscode-remote-try-java",
    use_llm=config["localization"]["llm_enabled"],
)
source = locator.extract_source(
    "microsoft_vscode-remote-try-java", "Java", ["getMessage", "evaluateNumber"],
)

# output_dir须为新的绝对路径；返回每轮累计测试覆盖层的target_project路径。
rounds = locator.assemble_tests(
    "microsoft_vscode-remote-try-java", plan,
    "D:/ScopeTrans-runs/microsoft-java-python",
)
```

实例绑定当前翻译对。换翻译对时重新创建实例；不会根据上一次调用猜仓库路径。

| 结果 | 关键字段 |
|---|---|
| 测试规划 | `{1: [{projectRoot, key}, ...], 2: [...], ...}`。每批仅包含新增测试；key为`{name, filePath, startLine}`，filePath相对于该引用的projectRoot。 |
| 测试组装 | `{1: "输出目录/round_001/target_project", ...}`。每轮包含截至当轮的累计公有测试，保留原目录结构和公有辅助代码、资源。 |
| 源码上下文 | `{status, content, warnings}`。content按依赖优先组织完整类／独立函数及项目内依赖的原文，包含必要导入、文件路径和片段行号；warnings记录歧义、未解依赖及外部依赖／资源提示。 |
| 错误与状态 | 规划和组装的输入、上游协议、原文不一致及引用错误抛出异常；语义未解沿用原保守分组并输出日志。源码提取仍返回原有ready／partial／ambiguous／not_found／error状态。 |

任务1只读取目标工程中自动发现的`public_test/public_tests`目录；Java深层包目录和Rust的`tests/public_tests`也会发现。辅助代码自动收集，无需逐文件配置。任务2读取同仓库`source_project`。不修改数据集源码；HTTP索引会写`.codegraph`元数据。Python客户端兼容上游函数清单返回三元key或旧版原生节点，以及新版getLocation返回结构。

两接口之间的跨语言函数名映射由总编排另行提供。阶段顺序来自公有测试证据，不等于证明源实现没有隐藏依赖；静态提取也不能保证解析反射或所有动态派发。

## 可选LLM

总控制台决定`use_llm`；关闭或没有可询问问题时不发请求。可以注入已有模型适配器：

```python
def llm_call(messages):
    response = existing_client.chat.completions.create(model=model_name, messages=messages)
    return {"content": response.choices[0].message.content,
            "usage": response.usage.model_dump() if response.usage else None}

locator = Localization(config, llm_call=llm_call)
```

未注入适配器时，模型连接信息统一填写在仓库根目录`.env`；YAML仅保留`localization.llm_enabled`开关。

```dotenv
# 地址：Chat Completions完整URL；也兼容服务根地址或/v1根地址。
LLM_ENDPOINT=https://your-provider.example/v1/chat/completions
# 模型名称
LLM_MODEL=your-model-name
# API密钥
LLM_API_KEY=your-api-key
LLM_TIMEOUT_SECONDS=120
LLM_MAX_OUTPUT_TOKENS=16384
LLM_TEMPERATURE=0
# 可选，仅在服务支持thinking参数时填写。短JSON任务可避免推理耗尽输出额度。
# LLM_THINKING=disabled
```

首次部署时执行`Copy-Item .env.example .env`，已有文件则直接编辑，不重复覆盖。进程中同名环境变量优先于文件；固定读取模块所在工程根目录，不受启动工作目录影响，不修改全局环境。

关闭LLM、没有可询问问题或使用注入适配器时不读取`.env`。接入总控制台时可提前加载同名环境变量或注入统一适配器。`.env`不会上传，`.env.example`可上传。模型建议校验和失败处理保持不变。

## 本地接口检查

开发配置`localization.dev.yaml`、`dev/localization/`中的脚本、测试和输出均不上传，也不是生产依赖。本机使用开发检查需安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe .\dev\localization\check_interfaces.py plan --repo microsoft_vscode-remote-try-java
.\.venv\Scripts\python.exe .\dev\localization\check_interfaces.py assemble --repo microsoft_vscode-remote-try-java
.\.venv\Scripts\python.exe .\dev\localization\check_interfaces.py source --repo microsoft_vscode-remote-try-java --functions getMessage evaluateNumber
```

脚本默认读取根目录本地配置，将JSON与report.txt写入忽略上传的开发输出目录。assemble另保存rounds.json及累计测试文件；source另将content原样保存为source_context.md，partial等非ready结果退出码为2。重新部署后生产接口可直接调用，本地检查文件需另行准备。

公有支持闭包保留GoogleTest参数化注册与Rust辅助类型的impl和常量；未解调用仍在内部标记或合并相关功能组，并输出诊断日志。源码提取补齐C++模板类外定义、C#泛型接口和Python项目内星号导入。

LLM仅接收未解项直接对应的结构化证据、已有功能与用例映射和必要公有原文；被完整辅助类型包含的方法原文不重复发送。内部长标识映射为短ID，响应恢复原ID后继续校验；不以丢弃测试或截断源码缩短请求。

源码依赖仍可能保守多提取，动态或反射未解时保留partial；测试批次来自公有证据，不是已证明完整的源调用图。

## 接口1输出

输入仍为plan_tests(translation_pair, repository_name, use_llm)，不增加配置或参数：

```python
for batch, tests in plan.items():
    for ref in tests:
        print(batch, ref["projectRoot"], ref["key"])
```

静态分析、可选LLM、内部功能分组、候选关系和分层策略继续沿用旧版。对外只保留批次和可查询引用；每例只出现一次，不拆测试断言。无法唯一绑定上游节点时明确报错，不伪造key或丢弃用例。JSON保存后批次编号会变为字符串，组装接口可读入该结果。

## 累计测试文件

```text
output_dir/
  round_001/
    manifest.json
    target_project/原公有测试相对路径/...
  round_002/
    manifest.json
    target_project/原公有测试相对路径/...
```

第N轮只保留第1至N批用例的完整正文，将后轮测试声明替换为空白，保留行号、原编码和换行；Python空语句块补pass。非测试定义、fixture、导入、资源和文件相对路径保留。manifest记录新增及累计用例引用和公有根目录。输入计划须覆盖当前全部已识别公测，不允许重复、遗漏或外部引用；不覆盖已有输出目录。

生成的target_project是测试覆盖层：将其文件按相对路径放入当轮译后工程，沿用总架构提供的统一测试配置执行。它不包含业务实现、私有测试、构建工具或第三方依赖。顶层导入和辅助代码保持原行为，其要求的业务接口仍须由当轮工程提供。准备每轮环境时完整替换公有测试目录，避免旧文件残留；目录外的私有测试不属于本轮公测执行范围。

## 接口2输出

```python
result = locator.extract_source(repository_name, source_language, function_names)
# {"status": "ready", "content": "源语言、请求入口与依赖优先排列的源码原文", "warnings": []}
source_context = result["content"]
```

输入不变。content用代码块展示原文，按文件标明完整类／函数与导入上下文；同一单元跨文件的partial类型、C++声明和类外实现分别保留。同一依赖片段只出现一次；循环依赖保留在同一依赖层。必要导入沿用原提取结果，不补写或改写业务代码。该内容供模型阅读，不是可以直接执行的合并源码文件。

| status | content与调用方处理 |
|---|---|
| ready | 已完成当前静态提取与原文核对；可使用content，外部依赖和资源仍见warnings。 |
| partial | 返回已确认源码，同时在warnings列出未解项；调用方需处理这些限制后再决定是否翻译。 |
| ambiguous | 入口同名歧义；content为空，warnings列出候选位置，调用方可改用限定名称重新查询。 |
| not_found | 未找到请求入口；content为空，warnings给出缺失名称。 |
| error | 输入、上游查询或原文一致性检查失败；content为空，warnings给出原因。 |

完整中间结果保留入口解析、源码单元、依赖边及依赖批次，使用localization.api的DEBUG日志查看；不作为简化接口的返回字段。
