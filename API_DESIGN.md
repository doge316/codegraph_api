# 接口设计与实现

## 业务函数

| 方法 | 输入 | 输出与职责 |
| --- | --- | --- |
| `initialize(options)` | 可选语言范围：字符串、数组或 null | 新仓库建库，返回 SDK 的 IndexResult 或 SyncResult，并创建符号缓存 |
| `sync()` | 无 | 按原语言范围更新，返回 SDK 的 SyncResult，并刷新符号缓存 |
| `getCallees(key)` | `{ name, filePath, startLine }` | 返回 `{ entry, functions, edges }`，仅包含直接调用 |
| `getContent(input)` | 同上，或 `{ kind: 'file', filePath }` | 返回 `{ node, filePath, source }` |
| `getAllFunctions()` | 无 | 返回函数和方法的 NodeKey 数组 |
| `getLocation(key)` | `{ name, filePath, startLine }` | 返回 `{ key, filePath, container }`，查询函数或方法的定义归属 |
| `getDependencies(level, input)` | class: `NodeKey`；file: `FileSelector` | 返回 `{ entry, dependencies }`，按目标去重的直接 class/file 依赖 |
| `getSourceUnit(input)` | `NodeKey` 或 `FileSelector` | CodeGraph 基础版：完整文件上下文及已解析文件级直接符号依赖 |
| `getTestAnalysis()` | 无 | CodeGraph 基础版：测试候选、显式辅助依赖闭包及图关系事实 |

所有方法返回 Promise。`getAllFunctions`、`getCallees`、`getLocation` 的符号返回值使用三元 key；调用边仍保留 SDK 原生结构，边的 source/target 仍为内部节点 ID。`getContent` 返回原生节点，完整文件没有对应 file 节点时，`node` 可以为 null。

## HTTP 调用层

接口文档由 `@elysia/openapi` 自动生成：`GET /doc` 提供 Scalar 交互页面，`GET /doc/json` 提供 OpenAPI JSON。请求结构来自路由的 Elysia Schema，字段包含中文说明及示例；路由 `detail` 补充中文功能说明、分组和状态码。`src/openapi.js` 集中定义文档信息及 SDK 响应结构，通过 OpenAPI 元数据展示，不改变业务响应校验或返回内容。SDK 节点和索引结果允许额外字段，保留原生返回结构。

通过 `npm start` 在 Node.js 24 上启动 Elysia 与 `@elysia/node` 适配器。默认监听 `127.0.0.1:3000`，可配置 `HOST` 和 `PORT`。服务启动不传仓库路径；保留现有 JS 导出。

| POST 路径 | JSON 请求体 |
| --- | --- |
| `/initialize` | `{ projectRoot, options?: { language } }` |
| `/sync` | `{ projectRoot }` |
| `/getAllFunctions` | `{ projectRoot }` |
| `/getCallees` | `{ projectRoot, key }` |
| `/getContent` | `{ projectRoot, input }` |
| `/getLocation` | `{ projectRoot, key }` |
| `/getDependencies` | `{ projectRoot, level, input }` |
| `/getSourceUnit` | `{ projectRoot, input }`，兼容 `{ projectRoot, key }` |
| `/getTestAnalysis` | `{ projectRoot }`，projectRoot 为公有测试目录 |

请求使用 `Content-Type: application/json`。`projectRoot` 必须是服务所在机器上的现有仓库绝对路径。`key` 是三元 key，`input` 是三元 key 或文件选择器。初始化支持省略 options，语言参数沿用现有业务规则。

`RepositoryManager.getRepository(projectRoot)` 校验路径、执行 `realpath()`，以真实路径为 Map key 获取或创建实例。所有路由使用同一管理对象，同一仓库复用实例和内存缓存；已有仓库可直接查询，重启服务后首次查询从持久缓存恢复，不要求再调用 initialize。不增加操作队列，使用方安排更新与查询分开进行。

HTTP `200` 直接返回业务结果的 JSON。错误统一为 `{ error: { code, message } }`，参数或 JSON 无效返回 `400`，资源不存在返回 `404`，仓库未初始化、已初始化或节点歧义返回 `409`，其他内部错误返回 `500`。歧义附带 `error.candidates`；建库错误存在原始结果时附带 `error.result`。SDK 当前没有为已初始化/未初始化提供错误码，HTTP 层根据固定 SDK 版本的对应错误消息映射，业务模块不变。

新增代码职责：`server.js` 负责启动与退出，`http-api.js` 负责路由、参数及错误转换，`repository-manager.js` 负责实例复用。HTTP 层没有增加数据库表、节点查询方法或缓存格式，也不负责上传代码文件。

## 初始化与更新

新增分析接口复用 `withGraph(..., true)`，要求目录已初始化，读取当前索引，不触发初始化、同步或测试执行。源码变动后先同步。只使用 CodeGraph SDK 和文件读取，不引入语言解析或框架收集依赖。

## 基础分析接口的实际边界

详细边界与反例见 [分析接口边界说明](./ANALYSIS_BOUNDARIES.md)。

`src/analysis-queries.js` 实现两个基础接口。与完整需求的差异通过返回的字符串 warnings 明确说明：

- getSourceUnit 按 key 精确查找 SDK 节点，支持五类缓存之外的常量、别名等索引实体；同 key 多匹配返回 AMBIGUOUS_NODE。为保留上下文返回完整所在文件（含其他声明），不假定 SDK 能补齐跨文件实现。dependencies 是该文件中 calls/extends/implements/instantiates/references/type_of/returns/imports 已解析边的外部符号目标，按 key 去重；同文件实体已覆盖，file 目标没有可继续提取的符号 key 时提示。
- getTestAnalysis 按函数/方法节点名称及 SDK decorators 识别候选，不执行 pytest 收集。辅助实体按测试文件及 test/helper/fixture 路径启发式分类，从入口求已解析关系闭包并防止循环。interfaces 仅记录入口直接 calls/instantiates 指向的非辅助实体名称，分类不是已确认的业务身份。
- 每个 case 的 contents 保留入口及已解析辅助实体所在文件全文。facts 使用图边 kind、owner key、target key 或 null、consumer 名称、filePath；仅当 SDK 边提供有效 line 时返回 startLine，不从函数定义行伪造调用位置。缺少对象数据流，不输出 producer/传参位置/endLine 等推断字段。
- warnings 始终包含覆盖限制；全局没有识别到测试也返回提示。未解析边目标增加具体警告。fixture 注入、autouse、参数化、类外实现和 partial 完整性均未实现。此版本不能等同于《需求补充.md》的完整分析方案。

## 初始化与更新流程

`initialize()` 只用于尚未建立数据库的目标仓库。通过 `CodeGraph.init(root, { index: false })` 建库，保存语言策略到目标仓库 `.codegraph/repository-api.json`。不限制语言时执行 `cg.indexAll()`；限制语言时发现指定语言文件，执行原生 `cg.sync({ paths })`。全量建库返回失败时抛错，原始结果保存在 `error.result`。

`sync()` 读取保存的语言策略。无限制时使用 `cg.sync()`；有限制时合并当前语言文件与数据库已有文件路径，识别新增、修改和删除。查询不会自动同步源码。

两个流程完成索引操作后，复用当前 SDK 连接执行内部 `refreshNodeCache(cg)`。原生 SyncResult 的 `failedFilePaths` 仍需调用者检查；缓存反映数据库当前状态。

## 五类符号缓存

- 内部 `loadSymbolNodes(cg)` 分别获取 function、method、class、struct、interface 五类完整节点，不复用或扩大 `getAllFunctions()` 的含义。
- 持久化位置：目标仓库 `.codegraph/nodes-cache.json`，结构为 `{ version: 1, nodes: [...] }`。
- 写入过程：先写 `.codegraph/nodes-cache.json.tmp`，完成后重命名替换正式文件，再使用新内存索引。正常替换不先删除正式文件。
- 内存 Map 的 key 是 `JSON.stringify([name, normalizedFilePath, startLine])`，value 为原生节点数组，保留同 key 的全部候选。
- 新实例首次符号查询读取 JSON；缓存缺失、损坏、结构无效或版本不兼容时，从现有数据库重建，不要求重新初始化。
- 更新开始清空当前实例内存；更新或缓存刷新抛错时使磁盘缓存失效，错误继续向上传播。下次查询按数据库现状重建。

缓存保存符号元数据。key 匹配不访问数据库；后续调用关系和源码查询仍使用 SDK。多次并发的首次查询共用一次缓存加载。不支持同一仓库并发更新与查询，也不自动刷新其他实例已有内存缓存；更新后其他实例应重新创建。不要混用外部 CLI/SDK 更新数据库；本模块不检测外部更新或进程突然中断后留下的旧缓存。

## 内部匹配与查询

`findNode(key)` 仅供内部使用，用户不必单独调用。名称精确匹配，路径必须相对仓库根目录，统一分隔符并归一化 `./`，拒绝绝对路径及越界路径。起始行必须为正整数，与 SDK 的 1 起始行号一致。

1. 没有匹配：抛 `NODE_NOT_FOUND`。
2. 一个匹配：返回索引中的完整节点，后续使用其 `id`。
3. 多个匹配：抛 `AMBIGUOUS_NODE`，`error.candidates` 提供候选完整节点，不自动选第一个。

`getCallees(key)` 匹配后校验节点是 function 或 method。读取 `cg.getOutgoingEdges(node.id)`，仅保留 calls 边；直接目标按 ID 去重并排除入口，自递归的边仍保留。entry 和 functions 中的节点投影为三元 key，edges 保留原生边。只返回图中已解析的直接调用，不递归查询间接调用。

`getContent(key)` 匹配后校验节点属于五类符号，再用 `await cg.getCode(node.id)` 获取源码。完整文件选择器绕过符号匹配，通过文件系统读取 UTF-8 内容，并查询可选的 file 节点；原有路径边界检查保留。

`getAllFunctions()` 仍合并 function 和 method，按文件、起始行、ID 排序后投影为三元 key，不负责初始化缓存。

`getLocation(key)` 复用 `findNode(key)` 并校验节点为 function 或 method。文件直接取 `node.filePath`；通过 `cg.getAncestors(node.id)` 按从近到远的顺序查找首个 class、struct 或 interface，作为 `container`。返回 `{ key, filePath, container }`，key 和非空 container 均为三元 key，不增加名称索引或变更缓存格式。嵌套类选择最近的所属类型，方法内的嵌套函数沿祖先关系查找所属类型。未找到时 `container` 为 null，包括文件级函数或 SDK 未记录完整包含关系的情况；不据此推断源码中一定没有所属类。不存在和歧义错误沿用现有 key 查询规则。

`getDependencies(level, input)` 只返回 `{ entry, dependencies }`，dependencies 按目标去重，不返回边种类或数量。class 层输入必须唯一匹配 class、struct 或 interface，扫描 SDK 可解析边并将符号端点映射到自身或最近所属类型，聚合 `calls`、`extends`、`implements`、`instantiates`，排除同类型依赖。file 层要求仓库内存在的文件选择器，按 `filePath` 聚合 `imports` 及上述符号关系，排除同文件关系。class 目标为 NodeKey，file 目标为 FileSelector。只返回直接关系，不计算传递依赖；关系缺失时结果为空数组。

无效输入或类型不适用抛 `INVALID_SELECTOR`；源码不可用抛 `SOURCE_NOT_FOUND`。SDK 和文件系统错误直接传播。

## 使用示例

```javascript
import { createRepository } from './src/index.js';

const repository = createRepository('/absolute/path/to/repository');
await repository.initialize({ language: 'python' }); // 仅新仓库执行一次

// 名称、路径和行号按实际源码填写，不需要先获取节点 ID。
const key = { name: 'entry', filePath: 'service.py', startLine: 4 };
const calls = await repository.getCallees(key);
const content = await repository.getContent(key);
const location = await repository.getLocation(key);
const classContent = await repository.getContent({
    name: 'Worker', filePath: 'service.py', startLine: 7
});
const fileContent = await repository.getContent({ kind: 'file', filePath: 'service.py' });
const functions = await repository.getAllFunctions();

// 返回项已经是 key，可以直接用于查询。
if (functions.length > 0) {
    await repository.getContent(functions[0]);
}

// 源码修改后同步，随后使用新的起始行查询。
await repository.sync();
```
