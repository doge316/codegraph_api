import { Elysia, t } from 'elysia';
import { node } from '@elysia/node';
import codegraph from '@colbymchenry/codegraph';
import { normalizeLanguages } from './language-scope.js';
import { RepositoryManager } from './repository-manager.js';

const projectRoot = t.String({ minLength: 1 });
const nodeKey = t.Object({
    name: t.String({ minLength: 1 }),
    filePath: t.String({ minLength: 1 }),
    startLine: t.Integer({ minimum: 1 })
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
        }, { body: t.Object({
            projectRoot,
            options: t.Optional(t.Object({ language: t.Optional(t.Union([
                t.String({ minLength: 1 }),
                t.Array(t.String({ minLength: 1 }), { minItems: 1 }),
                t.Null()
            ])) }))
        }) })
        .post('/sync', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.sync();
        }, { body: rootBody })
        .post('/getAllFunctions', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getAllFunctions();
        }, { body: rootBody })
        .post('/getCallees', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getCallees(body.key);
        }, { body: keyBody })
        .post('/getContent', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getContent(body.input);
        }, { body: t.Object({
            projectRoot,
            input: t.Union([nodeKey, t.Object({ kind: t.Literal('file'), filePath: t.String({ minLength: 1 }) })])
        }) })
        .post('/getLocation', async ({ body }) => {
            const repository = await manager.getRepository(body.projectRoot);
            return repository.getLocation(body.key);
        }, { body: keyBody });
}
