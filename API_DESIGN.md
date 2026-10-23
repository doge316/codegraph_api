# 接口设计与实现

## 一、业务函数及功能

### 1. `initialize(options)` —— 建库

- 为仓库建立 CodeGraph 数据库，索引源码并解析引用关系。
- `options.language` 省略或为 `null`：索引 SDK 支持的全部语言；传字符串或数组：只索引指定语言。
- 只能对**尚未建库**的仓库执行一次；已有数据库应改用 `sync()`。

### 2. `sync()` —— 增量更新

- 复用建库时保存的语言范围，发现文件新增、修改、删除并更新引用关系。
- 源码修改后、查询前，应先调用本函数。

### 3. `getCallees(node)` —— 查询直接调用关系

- 传入一个入口函数/方法节点，返回它**直接调用**的函数/方法。
- 返回结构：

```javascript
{
    entry:       /* 入口的原生 Node 对象 */,
    functions:   /* 直接调用的 function/method Node[]，排除入口 */,
    edges:       /* 入口的原生 calls Edge[] */
}
```

### 4. `getContent(node)` —— 读取源码

- 传入函数、方法、类、结构体、接口节点读取其源码；读整个文件传 `{ kind: 'file', filePath: 'src/core.py' }`。
- 返回结构：

```javascript
{
    node:       /* 原生 Node；整个文件无对应 file 节点时为 null */,
    filePath:   /* 相对仓库根的文件路径 */,
    source:     /* 源码字符串 */
}
```

### 5. `getAllFunctions()` —— 列出全部函数

- 返回 `Node[]`，合并 SDK 的 `function` 和 `method` 节点，按文件、起始行、ID 排序。
- 每项保留 SDK 原生字段：`id`、`name`、`qualifiedName`、`kind`、`filePath`、`language`、`startLine`、`endLine` 等。

---

## 二、实现思路

### `initialize` 的实现

1. 确定语言，如果是null，直接执行cg.indexAll()
2. 如果有限定语言，遍历文件，对每个文件调用 SDK 的 `detectLanguage()`，把语言属于选中集合的文件收进列表，再执行 `cg.sync({ paths })`

### `sync` 的实现

1. 使用SDK的sync()

### `getCallees` 的实现

1. 校验传入节点是函数或方法。
2. 取 `cg.getOutgoingEdges(node.id)`，只保留 `kind === 'calls'` 的边，返回每条边指向的函数/方法。

### `getContent` 的实现

- **整个文件**： `fs.readFile` 读完整 UTF-8 内容，并用 `getNodesInFile` 找可选的 `file` 节点。
- **符号**：校验传入节点的 kind 属于函数/方法/类/结构体/接口，再 `await cg.getCode(node.id)` 按 SDK 记录的区间取源码；取不到抛 `SOURCE_NOT_FOUND`。

### `getAllFunctions` 的实现

- 直接合并 `cg.getNodesByKind('function')` 与 `cg.getNodesByKind('method')`，按文件、起始行、ID 排序后返回。

---

## 三、调用五个公共方法

所有方法均返回 Promise，应放在 `async function` 中配合 `await` 使用。

需要先创建repository 实例。

### 1. 初始化

```javascript
await repository.initialize();
// 或者只选一种/多种语言：
await repository.initialize({ language: 'python' });
await repository.initialize({ language: ['cpp', 'c'] });
```

这些是不同新仓库的备选写法，不能对同一数据库连续初始化。已有数据库使用 `sync()`。

### 2. 更新

```javascript
const result = await repository.sync();
```

### 3. 获取调用关系

```javascript
const functions = await repository.getAllFunctions();
const total = functions.find(function (node) { return node.name === 'total'; });
const result = await repository.getCallees(total);
```

返回入口直接调用的函数/方法。结果包含：

| 字段          | 内容                                                                        |
| ------------- | --------------------------------------------------------------------------- |
| `entry`     | 入口的原生节点对象                                                          |
| `functions` | 直接调用的函数和方法节点数组                                                |
| `edges`     | 入口的原生 `calls` 边数组，边中含 `source`、`target`、`kind` 等字段 |

### 4. 获取内容

```javascript
const functions = await repository.getAllFunctions();
const add = functions.find(function (node) { return node.name === 'add'; });
const functionContent = await repository.getContent(add);

// 类节点需要调用方自行获得（例如通过 SDK 的 getNodesByKind('class')）。
const classContent = await repository.getContent(classNode);

const fileContent = await repository.getContent({ kind: 'file', filePath: 'service.py' });
```

返回 `{node, filePath, source}`。`source` 是源码字符串；`node` 是索引中的原生节点，完整文件没有对应文件节点时为 `null`。支持函数、方法、类、结构体、接口和文件。

### 5. 获取全部函数

```javascript
const functions = await repository.getAllFunctions();
const content = await repository.getContent(functions[0]);
```

返回 `Node[]`，包含图中的 `function` 和 `method`，也包含已入库的测试函数；每项保留 SDK 原生字段，例如 `id`、`name`、`kind`、`filePath`、`startLine`。这些节点可直接作为 `getCallees` 和 `getContent` 的输入。
