# CodeGraph JavaScript Repository API

基于官方 `@colbymchenry/codegraph@1.6.2` SDK，实现建库、同步、调用查询、源码读取和函数清单五个接口。运行代码为普通 ESM JavaScript。[`src/codegraph-repository.js`](./src/codegraph-repository.js) 是唯一的实现类；[`src/contracts.d.ts`](./src/contracts.d.ts) 提供对应的编辑器类型声明，不需要编译 TypeScript。

## 安装和使用

```bash
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

    // 3. 获取所有 function/method 节点，作为查询入口。
    const functions = await repository.getAllFunctions();
    const entry = functions.find(function (node) { return node.name === 'entry'; });
    const add = functions.find(function (node) { return node.name === 'add'; });

    // 4. 传入入口节点，获取它直接调用的函数/方法。
    const calls = await repository.getCallees(entry);

    // 5. 传入函数节点读取源码；读整个文件用 { kind: 'file', filePath }。
    const content = await repository.getContent(add);

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
const functions = await repository.getAllFunctions();
const entry = functions.find(function (node) { return node.name === 'entry'; });
const result = await repository.getCallees(entry);
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
const functions = await repository.getAllFunctions();
const add = functions.find(function (node) { return node.name === 'add'; });
const functionContent = await repository.getContent(add);

// 类节点需要调用方自行获得（例如通过 SDK 的 getNodesByKind('class')）。
const classContent = await repository.getContent(classNode);

const methodContent = await repository.getContent(
    functions.find(function (node) { return node.kind === 'method'; })
);

const fileContent = await repository.getContent({ kind: 'file', filePath: 'service.py' });
```

符号查询传入完整的原生 `Node`；整个文件查询传 `{ kind: 'file', filePath }`。函数和方法节点可直接从 `getAllFunctions()` 得到。

返回结构：

```javascript
{
    node: /* 原生 Node；整个文件未对应 file 节点时可以为 null */,
    filePath: 'core.py',
    source: 'def add(a, b):\n    return a + b'
}
```

函数/方法/类使用 `await cg.getCode(id)`，按 SDK 的源码区间读取；整个文件通过文件系统读取完整 UTF-8 内容。返回的 `source` 是本接口组织的字段，不是往原生 Node 上添加的属性。文件路径相对于仓库根目录，不能越出仓库。

## 5. 获取项目中全部函数

```javascript
const functions = await repository.getAllFunctions();
```

返回 `Node[]`，合并 `cg.getNodesByKind('function')` 和 `cg.getNodesByKind('method')`，按文件、起始行和 ID 排序。保留原生字段，包括 `id`、`name`、`qualifiedName`、`kind`、`filePath`、`language`、`startLine`、`endLine` 等。

这里的“全部”指数据库中的函数和方法节点，包含已入库测试函数；不包含作为其他节点类型表示的实体，也不绕过初始化时选择的语言范围。

## 节点输入与错误

`getCallees` 和 `getContent` 接收调用方已经拥有的完整原生 `Node`，不再按 `name` / `qualifiedName` / `id` 二次查找。函数和方法节点可从 `getAllFunctions()` 获取；类等其他节点由调用方自行提供。

`getCallees` 的入口必须是函数或方法，否则抛 `INVALID_SELECTOR`。`getContent` 接受函数/方法/类/结构体/接口节点，或 `{ kind: 'file', filePath }` 文件选择器；文件路径不能越出仓库。

无效输入抛 `INVALID_SELECTOR`；源码不可用抛 `SOURCE_NOT_FOUND`。SDK 和文件系统错误直接传播。

## 代码职责

| 文件                            | 职责                                                  |
| ------------------------------- | ----------------------------------------------------- |
| `src/contracts.d.ts`          | 编辑器类型声明                                        |
| `src/index.js`                | 导出实现类和工厂函数                                  |
| `src/codegraph-repository.js` | 五个公共方法的实现，SDK 生命周期、同步流程与策略保存  |
| `src/language-scope.js`       | 语言参数校验、文件语言筛选                            |
| `src/graph-queries.js`        | 调用遍历和内容查询                                    |

`CodeGraphRepository` 是唯一的实现类，五个公共方法都在其中。调用方通过 `createRepository()` 工厂获得实例，不直接依赖具体实现。若以后更换图引擎，只需替换 `src/` 中的实现。没有依赖旧解析模块，也没有读取或改写 CodeGraph 的内部 SQL 表。
