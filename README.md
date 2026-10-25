# CodeGraph JavaScript Repository API

基于官方 `@colbymchenry/codegraph@1.6.2` SDK，实现建库、同步、调用查询、源码读取、函数清单和定义归属六个接口。运行代码为普通 ESM JavaScript。[`src/codegraph-repository.js`](./src/codegraph-repository.js) 实现业务接口；[`src/contracts.d.ts`](./src/contracts.d.ts) 提供对应的编辑器类型声明，不需要编译 TypeScript。

## 安装和使用

运行环境要求 **Node.js 24.x**，当前已在 Node.js 24.15.0 上验证。SDK 的数据库实现依赖内置 `node:sqlite`，请先确认 Node 版本再安装。

```bash
node --version # 应为 v24.x.x
cd codegraph_api
npm ci
```

`src/index.js` 导出 `CodeGraphRepository` 和 `createRepository()`。应用代码调用工厂创建实现：

```javascript
import { createRepository } from './codegraph_api/src/index.js';

async function main() {
    const repository = createRepository('/absolute/path/to/repository');

    // 1. 初始化。目标目录应当已经存在，且尚未建立 CodeGraph 数据库。
    await repository.initialize({ language: 'python' });

    // 2. 以后修改源码后，调用 SDK 的 sync 更新数据库。
    await repository.sync();

    // 3. 获取所有 function/method 节点（可选，不是查询的前置步骤）。
    const functions = await repository.getAllFunctions();

    // 4. 名称、相对路径和行号按实际源码填写；行号从 1 开始。
    const calls = await repository.getCallees({
        name: 'entry', filePath: 'service.py', startLine: 4
    });

    // 5. 传入 key 读取源码；读整个文件用 { kind: 'file', filePath }。
    const content = await repository.getContent({
        name: 'add', filePath: 'service.py', startLine: 1
    });

    return { calls: calls, content: content, functions: functions };
}

main().catch(console.error);
```

所有接口均返回 Promise，通过 `await` 取结果。查询期间自动打开只读连接，操作结束自动关闭；调用方不需要维护 SDK 连接。

## 1. 初始化仓库

```javascript
await repository.initialize();                              // 不限制语言
await repository.initialize({ language: null });            // 不限制语言
await repository.initialize({ language: 'python' });        // 只选 Python
await repository.initialize({ language: 'cpp' });           // 只选 C++
await repository.initialize({ language: 'java' });          // 只选 Java
await repository.initialize({ language: ['python', 'java'] });
```

以上是不同新仓库的调用示例，不能对同一已初始化仓库连续执行初始化。已有数据库会报错，后续应调用 `sync()`。

- 语言名称使用 SDK 的语言 ID，例如 `python`、`cpp`、`java`；不接受 `py`、`c++` 等别名。
- 默认不限制语言时，调用 `CodeGraph.init(root, {index: false})`，再执行 `cg.indexAll()`。
- 限定语言时，用 SDK 的 `detectLanguage()` 筛选文件，传入原生 `cg.sync({paths: ...})`，由 SDK 完成提取和引用解析。
- SDK 没有原生的 `language` 建库选项；该参数是此模块实现的策略。
- 策略保存在目标仓库的 `.codegraph/repository-api.json`，不会修改根目录 `codegraph.json`。
- `.gitignore` 和项目配置中的 `exclude` 仍由 SDK 执行；没有承诺把磁盘上的所有文件强制入库。

语言判断遵循 SDK。例如 `.h` 默认判定为 `c`，纯 `cpp` 策略不包含它。需要 C/C++ 混合源码时，可传 `['cpp', 'c']`；也可在仓库原有的 `codegraph.json` 中配置 `.h` 的语言映射。

初始化返回 SDK 的 `IndexResult`（不限制语言）或 `SyncResult`（限定语言）。不限制语言的建库如果返回失败或存在文件级错误，会抛出异常，原始结果保存在 `error.result`。索引失败可能留下部分数据库，修正问题后可调用 `sync()` 重试。

## 2. 更新仓库

```javascript
const repository = createRepository('/absolute/path/to/repository');
const result = await repository.sync();
```

内部执行官方 SDK 的 `cg.sync()`，包括文件新增、修改、删除和引用关系更新，不使用子进程拼接 CLI 命令。该 SDK 方法承担 `codegraph sync` 对应的更新职责。

限定语言的仓库会重新发现该语言的文件，并把数据库已有文件路径加入检查清单，因此能发现新文件和被删除的旧文件；跨进程重新创建 `repository` 对象后也会读取保存的策略。

返回原生 `SyncResult`，可检查 `filesAdded`、`filesModified`、`filesRemoved`、`failedFilePaths` 等字段。查询不会自动执行同步，代码修改后应先调用此接口。

语言策略由本模块维护。直接在仓库执行全量 CLI `sync` / `index`，或者使用其他 SDK 实例执行全量同步，不会读取本模块的策略，可能将其他语言加入数据库；随后本接口会报告语言范围不一致，要求重建。不要把两种更新入口混用。

## 3. 获取直接调用的函数

```javascript
const result = await repository.getCallees({
    name: 'entry', filePath: 'service.py', startLine: 4
});
```

返回结构：

```javascript
{
    entry: /* 入口的原生 Node 对象 */,
    functions: [/* 直接调用的 function/method 原生 Node 对象 */],
    edges: [/* 入口的原生 calls Edge 对象 */]
}
```

例如 `entry -> helper -> total -> add`：`functions` 为 `[helper]`，不包含间接调用的 `total`、`add`。

实现取 `cg.getOutgoingEdges(entry.id)` 中的 `calls` 边，返回直接调用目标。保留实际调用边用于还原调用位置，函数结果按节点 ID 去重，并排除入口自己；自递归调用边保留在 `edges` 中。方法之间、函数与方法之间的 `calls` 边使用同一流程。

这是**已构建图中的直接调用结果**。CodeGraph 构图漏掉的动态调用、宏调用等不会被补出；未解析到项目节点的外部库函数也无法返回源码节点。

## 4. 读取函数、类或整个文件

```javascript
const functionContent = await repository.getContent({
    name: 'add', filePath: 'service.py', startLine: 1
});
const classContent = await repository.getContent({
    name: 'Worker', filePath: 'service.py', startLine: 7
});
const methodContent = await repository.getContent({
    name: 'run', filePath: 'service.py', startLine: 8
});

const fileContent = await repository.getContent({ kind: 'file', filePath: 'service.py' });
```

符号查询传入 `{ name, filePath, startLine }`，行号按实际索引位置填写；不需要先获得节点或 ID。整个文件查询传 `{ kind: 'file', filePath }`。如果已拥有 `getAllFunctions()` 返回的节点，也可以传入，其三个 key 字段参与匹配，额外字段（包括 `id`）不参与匹配。

返回结构：

```javascript
{
    node: /* 原生 Node；整个文件未对应 file 节点时可以为 null */,
    filePath: 'core.py',
    source: 'def add(a, b):\n    return a + b'
}
```

函数/方法/类/结构体/接口使用 `await cg.getCode(id)`，按 SDK 的源码区间读取；整个文件通过文件系统读取完整 UTF-8 内容。返回的 `source` 是本接口组织的字段，不是往原生 Node 上添加的属性。文件路径相对于仓库根目录，不能越出仓库。

## 5. 获取项目中全部函数

```javascript
const functions = await repository.getAllFunctions();
```

返回 `Node[]`，合并 `cg.getNodesByKind('function')` 和 `cg.getNodesByKind('method')`，按文件、起始行和 ID 排序。保留原生字段，包括 `id`、`name`、`qualifiedName`、`kind`、`filePath`、`language`、`startLine`、`endLine` 等。

这里的“全部”指数据库中的函数和方法节点，包含已入库测试函数；不包含作为其他节点类型表示的实体，也不绕过初始化时选择的语言范围。

## 6. 查询函数或方法的定义归属

```javascript
const location = await repository.getLocation({
    name: 'run', filePath: 'src/worker.py', startLine: 8
});
// location = { node, filePath, container }
```

`node` 是匹配的完整函数或方法节点，`filePath` 是它在仓库中的相对路径。`container` 是 SDK 包含关系中最近的 class、struct 或 interface 完整节点，未找到时为 `null`。文件级普通函数通常返回 `null`；嵌套类的方法返回最近的所属类，方法内的嵌套函数沿祖先关系查找最近的所属类型。查询表示源码定义归属，不包含继承或调用关系。

先复用现有内存索引的 `findNode(key)`，再通过 `cg.getAncestors(node.id)` 查询归属，不新增缓存格式或名称索引。SDK 缺少包含关系时也可能返回 `null`，不能据此断言源码中没有所属类型。key 无效或匹配到非函数/方法时抛 `INVALID_SELECTOR`；没有匹配抛 `NODE_NOT_FOUND`；多个匹配抛 `AMBIGUOUS_NODE` 并提供 `error.candidates`。

## 节点 key、缓存与错误

`getCallees` 和符号形式的 `getContent` 接收 `NodeKey`：

```javascript
{ name: 'entry', filePath: 'src/service.py', startLine: 4 }
```

名称精确匹配；路径相对于目标仓库根目录，统一分隔符、归一化 `./`；起始行是从 1 开始的整数。不能使用绝对路径或越出仓库。三个字段都必须提供。

内部 `findNode(key)` 查询内存 Map：没有匹配时抛 `NODE_NOT_FOUND`；一个匹配时继续查询；多个匹配时抛 `AMBIGUOUS_NODE`，`error.candidates` 保存候选原生节点，不自动选择第一个。`findNode` 不作为用户接口导出。

`initialize()` 索引完成和 `sync()` 更新完成后，内部 `loadSymbolNodes(cg)` 加载 function、method、class、struct、interface 五类完整节点。`refreshNodeCache(cg)` 将 `{ version: 1, nodes: [...] }` 写入**目标仓库**的 `.codegraph/nodes-cache.json.tmp`，写完后重命名为 `.codegraph/nodes-cache.json` 替换旧文件，再建立内存索引。`getAllFunctions()` 仍只查询函数和方法，不承担缓存加载职责。

新实例首次按 key 查询时读取 JSON 并建立内存索引；缓存缺失、JSON 损坏、结构无效或版本不兼容时，从已有数据库重建，无须重新初始化。后续 key 到节点 ID 的匹配不查询数据库；调用边和源码仍由 SDK 读取。读取整个文件不依赖符号缓存。

更新开始清空当前实例内存；更新或缓存生成抛出异常时删除持久缓存并传播错误，后续查询从数据库重建。成功写入使用临时文件替换，不先删除正式文件。`sync()` 仍返回原生结果；如包含失败文件，缓存反映数据库已有节点，不代表每个源码文件都同步成功。

通过本模块修改索引后使用当前实例查询；其他已加载缓存的实例不会自动感知更新，应重新创建。不要并发更新和查询同一仓库，也不要混用外部 CLI/SDK 修改索引；本实现不检测外部更新或进程突然退出期间留下的旧缓存。

`getCallees` 只接受函数或方法；`getContent` 支持上述五类符号或文件选择器。无效输入或节点类型不适用抛 `INVALID_SELECTOR`；源码不可用抛 `SOURCE_NOT_FOUND`。SDK 和文件系统错误直接传播。

## 代码职责

| 文件                            | 职责                                                  |
| ------------------------------- | ----------------------------------------------------- |
| `src/contracts.d.ts`          | 编辑器类型声明                                        |
| `src/index.js`                | 导出实现类和工厂函数                                  |
| `src/codegraph-repository.js` | 六个业务方法的实现，SDK 生命周期、同步流程与策略保存  |
| `src/language-scope.js`       | 语言参数校验、文件语言筛选                            |
| `src/node-cache.js`           | 内部五类符号加载、持久缓存、内存索引与 key 匹配       |
| `src/graph-queries.js`        | 调用遍历、内容与定义归属查询                          |

六个业务方法都在 `CodeGraphRepository` 中。调用方通过 `createRepository()` 工厂获得实例。缓存模块仅供内部使用；没有读取或改写 CodeGraph 的内部 SQL 表。
