# getSourceUnit 与 getTestAnalysis 能力边界

本文说明当前 CodeGraph 基础版的实际行为，依据 `src/analysis-queries.js`，不代表《需求补充.md》中所有目标均已实现。当前没有额外的语言语法解析器、测试框架分析器或对象数据流分析器。

## 1. 使用前提与共同限制

- `projectRoot` 是服务端目录的绝对路径。目录需要先初始化索引；已有索引不必重复初始化。两个接口不自动建库或同步。
- 所有 `filePath` 相对于本次 projectRoot。测试目录与源工程使用不同根目录时，key 不能直接跨根目录使用。
- 符号与关系来自索引，contents 来自磁盘当前文件。源码变化后必须先 sync；当前没有快照一致性检查，分析期间也不要修改文件。
- 只使用 SDK 已记录的关系：`calls`、`extends`、`implements`、`instantiates`、`references`、`type_of`、`returns`、`imports`。SDK 定义某种边，不代表每种语言都能完整提取该关系。
- 没有查到边，只表示没有索引证据，不能证明没有依赖。根目录外源码、外部库、动态导入和反射可能无法解析。
- 不执行测试或导入测试模块，因此不会验证测试是否可运行、参数是否正确或环境是否齐全。

`getContent` 保留原有功能：按符号区间读取，或读取整个文件。新增接口不替换它。

## 2. getSourceUnit 实际返回什么

HTTP：`POST /getSourceUnit`，输入 `{ projectRoot, input }`。input 为三元 key 或 `{ kind: "file", filePath }`；也兼容 `{ projectRoot, key }`。

返回 `{ entry, contents, dependencies, warnings }`。

| 字段 | 当前行为 | 不应作出的假设 |
| --- | --- | --- |
| entry | 返回匹配符号的 key，或文件选择器 | entry 不是返回源码范围的精确描述 |
| contents | 返回符号或所属类型所在文件的完整原文 | 不是仅返回该函数或所属类，也不是精确计算后的最小上下文 |
| dependencies | 收集该文件全部节点的已解析出边，返回位于其他文件的符号 key | 不是仅针对 entry 的依赖，不保证覆盖全部必要依赖 |
| warnings | 原因字符串数组，始终声明索引及上下文限制 | 非空不一定意味着请求失败；也不能只按 warning 数量判断完整性 |

### 2.1 完整文件保留与最小上下文提取的区别

假设 `Order.java` 中包含 Order、Other 两个类，输入 Order.total 的 key：

- contents 返回整个 Order.java，包含导入、两个类及文件中的其他声明。
- 不会单独计算 total 实际需要哪些 import、常量或初始化语句。
- dependencies 可能包含 Other 使用的外部符号，即使 total 不使用它们。

这种做法减少了同文件上下文被截断的情况，但可能带入无关代码和额外依赖。需要精确方法原文时继续使用 getContent；需要完整单元的最小上下文时，当前版本仍不满足。

contents 每项是 `{ node, filePath, source }`。由于当前返回全文，node 是 SDK file 节点，缺失时为 null，不是输入符号或所属类节点。数组顺序是文件路径顺序，不表示执行或初始化顺序。

### 2.2 依赖范围

依赖按三元 key 去重并稳定排序。同文件实体已包含在全文中，不再列入 dependencies。没有有效符号 key 的目标（包括 file 节点）不会放进 dependencies，会增加提示。遍历中遇到边目标节点缺失也会提示。

依赖只是一层出边，不递归提取外部实现；调用方可以继续用返回的 key 查询 getSourceUnit。该接口直接查 SDK 文件节点，支持已索引的常量、别名等，不受原有五类符号缓存限制。但返回 key 仍可能存在歧义，再次查询时会返回歧义错误。

### 2.3 跨文件与多语言边界

| 场景 | 当前结果 |
| --- | --- |
| 普通函数或类位于一个文件 | 返回整个文件，以及索引确认的文件级外部依赖 |
| 方法属于类 | 可依据 SDK 祖先关系选取所属类型的文件，最终仍返回全文 |
| C++ Book.h 声明、Book.cpp 实现 | 不自动关联或合并，输入头文件符号时不能保证带上 cpp 方法体 |
| C# partial | 不自动查找并合并同一类型的其他部分 |
| 继承、字段或返回类型引用 | 仅当 SDK 记录了所选类型的边并解析出目标节点时才可返回 |
| 宏、条件编译、生成代码 | 不解释构建条件，不能确认当前源码是否为实际生效版本 |
| 动态配置、资源文件、环境变量 | 不追踪运行时值，不自动收集外部文件 |

因此本接口可以提供阅读、翻译的输入材料，但不能标记为“跨文件完整、可直接编译的独立单元”。

## 3. getTestAnalysis 实际返回什么

HTTP：`POST /getTestAnalysis`，输入 `{ projectRoot }`，projectRoot 为已建索引的公有测试目录。当前没有单测试文件过滤参数。

返回 `{ cases, warnings }`。每项 case 包含 entry、interfaces、dependencies、contents、facts、warnings；顶层 warnings 描述全局发现限制，case warnings 描述该项分析限制。

### 3.1 测试候选发现

只检查 SDK function/method 节点：名称为 test、以 test_ 开头、或 test 后接大写字母，以及 decorators 中匹配 Test、ParameterizedTest、RepeatedTest、Theory、Fact、test 的节点。

这些是候选规则，不是 pytest/JUnit 等框架的正式收集过程：

- 普通函数只要名称匹配，也可能被误认为测试。
- 自定义命名、框架配置、动态生成和 SDK 未记录的装饰器可能导致遗漏。
- 参数化测试不展开为参数实例；skip、xfail、测试生命周期和框架过滤规则不被解释。
- 没发现 candidates 时返回空 cases 和提示，不能断言目录没有测试。

### 3.2 interfaces 与 dependencies 如何区分

当前根据文件路径启发式判断辅助代码：包含候选测试的文件、conftest/helper/fixture 命名文件，以及 test/tests/helper/helpers/fixture/fixtures 目录中的实体，被归为辅助实体。

interfaces 只收集入口测试节点直接 calls/instantiates 指向的非辅助目标，名称优先使用 SDK qualifiedName。不会深入 helper 推导其最终调用的被测接口。

dependencies 从入口开始，递归遍历已解析关系中的辅助目标，按 key 去重并防止循环；不进入其他已识别的测试候选，不递归展开被判为被测接口的目标。

这会产生以下边界：

- 被测实现若与测试写在同一文件，可能被归为 helper，导致 interfaces 遗漏。
- 辅助代码如果放在不符合规则的路径，可能被误列为被测接口。
- projectRoot 只包含测试，而实现未进入索引时，调用目标可能无法解析，interfaces 为空。
- 无法依据上述分类证明某个目标确实是被测业务接口。

### 3.3 隐式 fixture 的具体缺口

```python
@pytest.fixture
def rate():
    return 0.9

def test_total(rate):
    assert total(rate) == 90
```

test_total 没有显式调用 rate()。如果 SDK 没记录注入关系，当前 dependencies 不会因此自动包含 rate。即使全文 contents 恰好包含了 rate 的定义，也不代表已确认 fixture 绑定关系。

放在 conftest.py 中的 fixture，只有存在可遍历的已解析辅助依赖时才可能进入结果；当前不会主动按参数名查找、解析作用域覆盖或扫描 autouse/usefixtures。fixture scope、yield 清理、间接参数化以及外部 pytest 插件都没有专门分析。

### 3.4 contents 与测试组装

contents 返回测试和已发现辅助实体所在文件的全文，按路径去重。它可能包含其他测试、同文件 fixture、参数数据及无关初始化代码。多个 case 可以重复携带同一个文件。

这里保留了原文，但没有确认每一段原文是否必要：

- 不会从全文精确拆出当前测试、必要 fixture、数据和导入。
- 不会收集未被图关系覆盖的 conftest、配置、外部数据、资源和插件。
- 不计算依赖拓扑顺序，不处理批次之间的状态隔离或 fixture 生命周期。
- 不能把所有 contents 直接拼接成一个文件：模块路径、重名实体、导入和其他测试可能冲突。

现阶段可以保留原模块结构进行人工检查或后续分析，不宜直接把响应当成“最小可运行批次”。

### 3.5 facts 是图关系证据，不是对象数据流

当前每项 fact 的结构为：

```json
{
  "kind": "calls",
  "owner": { "name": "test_total", "filePath": "test_order.py", "startLine": 3 },
  "target": { "name": "total", "filePath": "order.py", "startLine": 10 },
  "consumer": "Order.total",
  "filePath": "test_order.py",
  "startLine": 5
}
```

示例只说明字段结构，实际内容取决于 SDK 索引。owner 是发起关系的测试或辅助符号；target 是目标 key，没有有效 key 时为 null。fact.startLine 只在 SDK 边带有效行号时出现，不能用 owner.startLine 代替调用位置。

当前不返回 producer、variable、usedAs、argumentIndex、argumentName 或 endLine，因为没有证据确定对象来源、接收者、参数绑定和表达式结束行。facts 也可能包含 extends、references 等其他所选边，不全是调用。辅助递归路径产生的事实同样会出现在该 case 中，应通过 owner 区分归属。

不要把调用边解释为“变量由哪个构造器创建”或“第几个参数来自哪个 fixture”。这些仍需要后续语法与数据流分析。

## 4. warnings 与错误的区别

warnings 是字符串数组，目前始终含有基础限制提示；它不是完整的逐问题诊断系统。某些关系根本没有进入 SDK 图时，服务无法知道具体漏了哪一项，只能给出通用限制提示。

| 结果 | 含义 |
| --- | --- |
| HTTP 200 且有 warnings | 基础查询完成，结果仍有已知能力限制，不保证语义完整 |
| 空 interfaces/dependencies/cases | 当前规则与索引未找到对应结果，不证明实际不存在 |
| 400 | 请求结构或 key/path 无效、路径越界 |
| 404 | 文件或匹配节点不存在等资源错误 |
| 409 | key 歧义或仓库未初始化等状态冲突 |
| 500 | 其他 SDK、数据库或文件系统错误 |

当前不做逐 case 的错误隔离：某个所需文件读取失败，可能让整个请求失败，不会稳定返回其余 case 的部分结果。

## 5. 与完整需求的差距及后续方向

| 完整需求 | 当前状态 | 需要补充 |
| --- | --- | --- |
| 确认测试身份 | 名称/装饰器候选 | 框架规则与配置解析 |
| 隐式 fixture 和辅助依赖闭包 | 仅图中显式可达辅助实体 | fixture 可见性、覆盖、参数化和生命周期分析 |
| 必要上下文原文 | 保守返回全文 | 源码区间、作用域与引用解析 |
| 对象来源及使用/传参事实 | 只有 SDK 图关系 | AST、局部数据流和参数绑定分析 |
| 跨文件完整源码单元 | 不自动补齐 | 声明/定义关联与语言专用规则 |
| 功能分组、排序、批次组装 | 提供基础证据，不自动完成 | 校验接口身份、组装规则、执行隔离与运行验证 |

新增 AST/框架分析器后可以逐项提高准确度；在此之前，应把两个接口视为“基于现有图的材料与证据查询”，并始终保留 warnings 给下游查看。

## 6. 已验证范围

当前测试覆盖真实 CodeGraph 建库和 HTTP 调用、符号/文件输入、常量 key、getContent 兼容性、显式 helper 依赖、辅助循环去重、缺失目标提示、缺少调用行号时不伪造位置、路径校验和 OpenAPI 引用。

这些测试验证基础实现行为，不代表已验证所有语言、框架、动态测试或跨文件构建场景。本文描述的缺口不能因基础测试通过而视为已解决。
