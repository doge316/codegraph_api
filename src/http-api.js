import { Elysia, t } from 'elysia';
import { node } from '@elysia/node';
import { openapi } from '@elysia/openapi';
import codegraph from '@colbymchenry/codegraph';
import { normalizeLanguages } from './language-scope.js';
import { RepositoryManager } from './repository-manager.js';
import { apiDocumentation, routeDocumentation, resultSchema } from './openapi.js';

const projectRoot = t.String({ minLength: 1, description: '服务所在机器上的仓库绝对路径', examples: ['/absolute/path/to/repository'] });
const nodeKey = t.Object({
    name: t.String({ minLength: 1, description: '索引中的符号名称', examples: ['run'] }),
    filePath: t.String({ minLength: 1, description: '相对于仓库根目录的文件路径', examples: ['src/worker.py'] }),
    startLine: t.Integer({ minimum: 1, description: '索引中的符号起始行号，从 1 开始', examples: [8] })
});
const rootBody = t.Object({ projectRoot });
const keyBody = t.Object({ projectRoot, key: nodeKey });

function errorResponse(code, error) {
    if (code === 'VALIDATION' || code === 'PARSE') {
        return { status: 400, body: { error: {
            code: 'INVALID_SELECTOR',
            message: code === 'PARSE' ? 'Request body must be valid JSON.' : 'Request body does not match the API schema.'
        } } };
    }
    let status = 500;
    let errorCode = error.code || 'INTERNAL_ERROR';
    if (code === 'NOT_FOUND') {
        status = 404;
        errorCode = 'ROUTE_NOT_FOUND';
    } else if (errorCode === 'INVALID_SELECTOR') {
        status = 400;
    } else if (['NODE_NOT_FOUND', 'SOURCE_NOT_FOUND', 'ENOENT', 'ENOTDIR'].includes(errorCode)) {
        status = 404;
    } else if (errorCode === 'AMBIGUOUS_NODE') {
        status = 409;
    } else if (error.message?.startsWith('CodeGraph already initialized in ')) {
        status = 409;
        errorCode = 'ALREADY_INITIALIZED';
    } else if (error.message?.startsWith('CodeGraph not initialized in ')) {
        status = 409;
        errorCode = 'NOT_INITIALIZED';
    }
    const detail = { code: errorCode, message: error.message || 'Request failed.' };
    if (error.candidates) detail.candidates = error.candidates;
    if (error.result) detail.result = error.result;
    return { status, body: { error: detail } };
}

/** HTTP adapter for the existing six business methods. */
export function createHttpApi() {
    const manager = new RepositoryManager();
    return new Elysia({ adapter: node() })
        .use(openapi({ path: '/doc', specPath: '/doc/json', documentation: apiDocumentation }))
        .onError(({ code, error, set }) => {
            const response = errorResponse(code, error);
            set.status = response.status;
            return response.body;
        })
        .post('/initialize', async ({ body }) => {
            // Validate the language before asking the SDK to create a database.
            try {
                normalizeLanguages(body.options?.language, codegraph);
            } catch (error) {
                error.code = 'INVALID_SELECTOR';
                throw error;
            }
            const repository = await manager.getRepository(body.projectRoot);
            return repository.initialize(body.options);
        }, { detail: routeDocumentation('initialize', '初始化仓库', '仅用于尚未建库的仓库。索引完成后缓存 function、method、class、struct、interface 五类节点。', resultSchema.initialize, '仓库管理'), body: t.Object({
            projectRoot,
            options: t.Optional(t.Object({ language: t.Optional(t.Union([
                t.String({ minLength: 1 }),
                t.Array(t.String({ minLength: 1 }), { minItems: 1 }),
                t.Null()
            ], { description: '语言名称或非空语言数组；省略或 null 时索引全部支持的语言', examples: ['python', ['python', 'javascript'], null] })) }))
        }) })
        .post('/sync', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.sync();
        }, { body: rootBody, detail: routeDocumentation('sync', '同步代码索引', '沿用初始化语言策略更新索引并刷新节点缓存。等待同步完成后再查询。', resultSchema.sync, '仓库管理') })
        .post('/getAllFunctions', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getAllFunctions();
        }, { body: rootBody, detail: routeDocumentation('getAllFunctions', '获取所有函数和方法', '返回 function 和 method 的三元 key 数组，按内部节点的文件路径、起始行号和 ID 排序。返回值可直接用于后续查询。', resultSchema.getAllFunctions) })
        .post('/getCallees', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getCallees(body.key);
        }, { body: keyBody, detail: routeDocumentation('getCallees', '查询直接调用关系', 'key 必须唯一匹配函数或方法。返回入口 key、去重后的直接被调用函数 key 及原生 calls 边；边保留自递归调用。', resultSchema.getCallees) })
        .post('/getContent', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getContent(body.input);
        }, { detail: routeDocumentation('getContent', '获取符号或文件源码', 'input 支持五类符号的三元 key，或 { kind: "file", filePath }。完整文件没有对应 file 节点时 node 为 null。', resultSchema.getContent), body: t.Object({
            projectRoot,
            input: t.Union([nodeKey, t.Object({ kind: t.Literal('file'), filePath: t.String({ minLength: 1, description: '仓库内文件的相对路径', examples: ['src/worker.py'] }) })])
        }) })
        .post('/getLocation', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getLocation(body.key);
        }, { body: keyBody, detail: routeDocumentation('getLocation', '查询函数或方法的位置', '返回函数或方法的 key、相对文件路径及 SDK 记录的最近所属 class、struct 或 interface 的 key；无所属类型时 container 为 null。', resultSchema.getLocation) });
}
