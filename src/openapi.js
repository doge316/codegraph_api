// Documentation schemas describe SDK results without changing response validation.
const object = (properties, required = Object.keys(properties)) => ({
    type: 'object', properties, required, additionalProperties: true
});
const string = { type: 'string' };
const integer = { type: 'integer' };
const ref = (name) => ({ $ref: `#/components/schemas/${name}` });
const array = (items) => ({ type: 'array', items });
const nullable = (schema) => ({ anyOf: [schema, { type: 'null' }] });
const json = (schema, description) => ({
    description, content: { 'application/json': { schema } }
});

export const apiDocumentation = {
    info: {
        title: 'CodeGraph API',
        version: '1.0.0',
        description: '代码仓库初始化、同步与查询。projectRoot 必须是服务所在机器上的仓库绝对路径。已有仓库可直接查询；同一仓库更新完成后再查询，源码变动后需同步并重新获取 key。'
    },
    tags: [
        { name: '仓库管理', description: '创建和更新仓库索引' },
        { name: '代码查询', description: '查询函数、源码、调用关系及位置' }
    ],
    components: { schemas: {
        NodeKey: {
            ...object({
                name: { type: 'string', description: '符号名称' },
                filePath: { type: 'string', description: '仓库相对路径' },
                startLine: { type: 'integer', minimum: 1, description: '起始行号，从 1 开始' }
            }),
            additionalProperties: false,
            example: { name: 'run', filePath: 'src/worker.py', startLine: 8 }
        },
        Node: {
            ...object({
                id: string, kind: string, name: string, qualifiedName: string,
                filePath: string, language: string, startLine: integer, endLine: integer,
                startColumn: integer, endColumn: integer, updatedAt: { type: 'number' }
            }),
            description: 'SDK 原生节点，列出公共必需字段；可包含 signature、docstring 等额外字段。'
        },
        Edge: object({
            source: string, target: string, kind: string,
            metadata: { type: 'object', additionalProperties: true },
            line: integer, column: integer, provenance: string
        }, ['source', 'target', 'kind']),
        IndexResult: object({
            success: { type: 'boolean' }, filesIndexed: integer, filesSkipped: integer,
            filesErrored: integer, nodesCreated: integer, edgesCreated: integer,
            errors: array({ type: 'object', additionalProperties: true }),
            durationMs: { type: 'number' }
        }),
        SyncResult: object({
            filesChecked: integer, filesAdded: integer, filesModified: integer,
            filesRemoved: integer, nodesUpdated: integer, durationMs: { type: 'number' }
        }),
        CallResult: object({ entry: ref('NodeKey'), functions: array(ref('NodeKey')), edges: array(ref('Edge')) }),
        ContentResult: object({ node: nullable(ref('Node')), filePath: string, source: string }),
        LocationResult: object({ key: ref('NodeKey'), filePath: string, container: nullable(ref('NodeKey')) }),
        ErrorResponse: object({ error: object({
            code: string, message: string, candidates: array(ref('Node')),
            result: { type: 'object', additionalProperties: true }
        }, ['code', 'message']) })
    } }
};

export function routeDocumentation(operationId, summary, description, schema, tag = '代码查询') {
    return {
        operationId, summary, description, tags: [tag],
        responses: {
            200: json(schema, '操作成功，直接返回业务结果'),
            400: json(ref('ErrorResponse'), 'JSON、参数、语言或节点类型无效'),
            404: json(ref('ErrorResponse'), '仓库、文件、节点或源码不存在'),
            409: json(ref('ErrorResponse'), '未初始化、重复初始化或节点匹配歧义'),
            500: json(ref('ErrorResponse'), 'SDK、数据库或文件系统内部错误')
        }
    };
}

export const resultSchema = {
    initialize: { anyOf: [ref('IndexResult'), ref('SyncResult')] },
    sync: ref('SyncResult'),
    getAllFunctions: array(ref('NodeKey')),
    getCallees: ref('CallResult'),
    getContent: ref('ContentResult'),
    getLocation: ref('LocationResult')
};
