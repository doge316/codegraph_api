# 接口设计与实现

## 六个业务函数

| 方法 | 输入 | 输出与职责 |
| --- | --- | --- |
| `initialize(options)` | 可选语言范围：字符串、数组或 null | 新仓库建库，返回 SDK 的 IndexResult 或 SyncResult，并创建符号缓存 |
| `sync()` | 无 | 按原语言范围更新，返回 SDK 的 SyncResult，并刷新符号缓存 |
| `getCallees(key)` | `{ name, filePath, startLine }` | 返回 `{ entry, functions, edges }`，仅包含直接调用 |
| `getContent(input)` | 同上，或 `{ kind: 'file', filePath }` | 返回 `{ node, filePath, source }` |
| `getAllFunctions()` | 无 | 返回函数和方法的完整原生 Node 数组 |
| `getLocation(key)` | `{ name, filePath, startLine }` | 返回 `{ node, filePath, container }`，查询函数或方法的定义归属 |

所有方法返回 Promise。返回的节点和边保留 SDK 原生结构。完整文件没有对应 file 节点时，`node` 可以为 null。

## 初始化与更新

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

`getCallees(key)` 匹配后校验节点是 function 或 method。读取 `cg.getOutgoingEdges(node.id)`，仅保留 calls 边；直接目标按 ID 去重并排除入口，自递归的边仍保留。只返回图中已解析的直接调用，不递归查询间接调用。

`getContent(key)` 匹配后校验节点属于五类符号，再用 `await cg.getCode(node.id)` 获取源码。完整文件选择器绕过符号匹配，通过文件系统读取 UTF-8 内容，并查询可选的 file 节点；原有路径边界检查保留。

`getAllFunctions()` 仍合并 function 和 method，按文件、起始行、ID 排序，不负责初始化缓存。

`getLocation(key)` 复用 `findNode(key)` 并校验节点为 function 或 method。文件直接取 `node.filePath`；通过 `cg.getAncestors(node.id)` 按从近到远的顺序查找首个 class、struct 或 interface，作为 `container`。返回完整原生节点，不增加名称索引或变更缓存格式。嵌套类选择最近的所属类型，方法内的嵌套函数沿祖先关系查找所属类型。未找到时 `container` 为 null，包括文件级函数或 SDK 未记录完整包含关系的情况；不据此推断源码中一定没有所属类。不存在和歧义错误沿用现有 key 查询规则。

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

// 也可从已有节点提取 key；额外字段（包括 id）不参与匹配。
if (functions.length > 0) {
    const { name, filePath, startLine } = functions[0];
    await repository.getContent({ name, filePath, startLine });
}

// 源码修改后同步，随后使用新的起始行查询。
await repository.sync();
```
