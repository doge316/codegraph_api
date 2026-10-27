# ScopeTrans定位接口

两个Python接口共用书畅的CodeGraph HTTP服务：公有测试生成独立功能组、候选关系与批次，已知源函数名提取完整类／函数及依赖。不执行翻译、测试或文件编辑，不自动保存输出。

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
```

实例绑定当前翻译对。换翻译对时重新创建实例；不会根据上一次调用猜仓库路径。

| 结果 | 关键字段 |
|---|---|
| 测试规划 | `groups`保留独立功能组与完整cases/support/context/resources；`relations`列出候选组间关系；`batches`引用group_ids并带累计测试标识；`uncertainties`标记局部未解项。 |
| 源码上下文 | `units[].fragments[].source`是完整原文；`batches`按依赖优先排列单元key；一个单元可能跨多个文件片段。 |
| 状态 | `ready`表示无已知阻塞；`partial`保留有用结果和未确定项；歧义返回`ambiguous`，未找到返回`not_found`，其他失败返回`error`。 |

任务1只读取目标工程中自动发现的`public_test/public_tests`目录；Java深层包目录和Rust的`tests/public_tests`也会发现。辅助代码自动收集，无需逐文件配置。Java的`@MethodSource`静态方法引用和公有辅助类的完整定义纳入支持闭包；测试外层类只保留上下文，避免夹带后轮测试。任务2读取同仓库`source_project`。核心不修改源码；HTTP索引会写`.codegraph`元数据。

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
.\.venv\Scripts\python.exe .\dev\localization\check_interfaces.py source --repo microsoft_vscode-remote-try-java --functions getMessage evaluateNumber
```

脚本默认读取根目录本地配置，将完整JSON与阅读报告写入忽略上传的开发输出目录，`partial`等非ready结果退出码为2。重新部署后生产接口可直接调用，本地检查文件需另行准备。

公有支持闭包保留GoogleTest参数化注册与Rust辅助类型的impl和常量；未解调用只标记或合并相关功能组并返回partial。源码提取补齐C++模板类外定义、C#泛型接口和Python项目内星号导入。

LLM仅接收未解项直接对应的结构化证据、已有功能与用例映射和必要公有原文；被完整辅助类型包含的方法原文不重复发送。内部长标识映射为短ID，响应恢复原ID后继续校验；不以丢弃测试或截断源码缩短请求。

旧版量化基线保留在忽略上传的dev/localization/output/audit35，不能视作新输出的排序可靠率。源码依赖仍可能保守多提取，动态或反射未解时保留partial，不将候选阶段当成已证明的源调用图。

## 接口1输出

输入仍为plan_tests(translation_pair, repository_name, use_llm)，不增加配置或参数。原stages字段替换为以下结构：

```python
for group in plan["groups"]:
    print(group["id"], group["interfaces"], group["status"])
    # group["cases"]、support、context、resources保留完整公有材料。
for relation in plan["relations"]:
    print(relation["before"], "→", relation["after"], relation["kind"])
for batch in plan["batches"]:
    print(batch["index"], batch["group_ids"], batch["cumulative_case_keys"])
for item in plan["uncertainties"]:
    print(item["id"], item["group_ids"], item["message"])
```

相同业务接口集合的完整用例可跨文件归组；同层不同组保持独立。共享状态与循环只合并相关范围。relations的certainty均为candidate：test_fact来自公测数据流，interface_subset是较小接口集合优先的增量候选，llm_semantic_candidate来自模型语义建议；均不构成隐藏源码依赖的证明。同组内允许依赖，不拆测试断言。

批次是候选安排，同批不代表已证明可并行。G编号是标识，不是顺序；按batches读取顺序。接口2的输入输出不变。历史audit35结果保留旧结构，不覆盖为新版本测量结果。

当前79项回归通过（含12项真实HTTP、13项源码依赖精确性检查）。35个样本的静态／静态＋LLM评估位于dev/localization/output/interface1_eval_2026-10-26；按公测未发现反向依赖即通过的口径，两模式均为35/35，平均候选批次均为2.03。19个样本实际调用LLM；Rust计时器样本提高至16384输出上限后单独重测，8560 Token正常结束，最终19个建议响应均通过格式与引用校验。原2048失败记录及重测消耗另行保留。

接口2按原35个样本、69次查询和相同参考清单重测：入口定位69/69、完整单元提取141/141、必要依赖召回133/133；依赖精确率由133/202（65.84%）提高至133/133（100.00%）。C++合并实现时排除测试侧原型，保留真实头文件；Python按完整提取单元的实际引用筛选导入，保留可能的模块初始化、包初始化和动态访问依赖。结果限于本次参考清单，复杂动态情况仍保守保留并报告未解项。原35份接口1静态计划重跑比较无语义变化，上游代码未修改。本轮原始输出、对照及核查说明位于dev/localization/output/source_precision_v2_2026-10-26；没有新增LLM请求。

已解析接口按规范标识归组；未解接收者不因显示名称相同就强行视为同一功能，保留各自未解标记。候选批次不是实际执行顺序已获源码证明的结论。
