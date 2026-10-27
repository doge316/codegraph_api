import assert from 'node:assert/strict';
import test from 'node:test';
import { getAllFunctions, getCallees, getLocation, getDependencies } from '../src/graph-queries.js';
import { createHttpApi } from '../src/http-api.js';

const entry = { id: 'entry-id', kind: 'method', name: 'run', filePath: 'worker.py', startLine: 8, endLine: 12 };
const helper = { id: 'helper-id', kind: 'function', name: 'helper', filePath: 'helpers.py', startLine: 2 };
const container = { id: 'class-id', kind: 'class', name: 'Worker', filePath: 'worker.py', startLine: 1 };
const entryKey = { name: 'run', filePath: 'worker.py', startLine: 8 };
const helperKey = { name: 'helper', filePath: 'helpers.py', startLine: 2 };
const containerKey = { name: 'Worker', filePath: 'worker.py', startLine: 1 };

test('function listing returns sorted keys usable as query input', () => {
    const cg = { getNodesByKind: kind => kind === 'function' ? [helper] : [entry] };
    assert.deepEqual(getAllFunctions(cg), [helperKey, entryKey]);
});

test('callee results return keys while preserving deduplication and recursive edges', () => {
    const edges = [
        { source: entry.id, target: helper.id, kind: 'calls', line: 9 },
        { source: entry.id, target: helper.id, kind: 'calls', line: 10 },
        { source: entry.id, target: entry.id, kind: 'calls', line: 11 },
        { source: entry.id, target: container.id, kind: 'contains' }
    ];
    const cg = {
        getOutgoingEdges: id => { assert.equal(id, entry.id); return edges; },
        getNode: id => [entry, helper, container].find(node => node.id === id)
    };
    assert.deepEqual(getCallees(cg, entry), {
        entry: entryKey, functions: [helperKey], edges: edges.slice(0, 3)
    });
    assert.throws(() => getCallees(cg, container), { code: 'INVALID_SELECTOR' });
});

test('location returns entry and nearest type keys, or a null container', () => {
    const cg = { getAncestors: () => [entry, container, { ...container, id: 'outer', name: 'Outer' }] };
    assert.deepEqual(getLocation(cg, entry), { key: entryKey, filePath: 'worker.py', container: containerKey });
    assert.deepEqual(getLocation({ getAncestors: () => [] }, helper), {
        key: helperKey, filePath: 'helpers.py', container: null
    });
    assert.throws(() => getLocation(cg, container), { code: 'INVALID_SELECTOR' });
});

test('OpenAPI documents keys for all three changed responses', async () => {
    const app = createHttpApi();
    const response = await app.handle(new Request('http://localhost/doc/json'));
    assert.equal(response.status, 200);
    const spec = await response.json();
    const schemas = spec.components.schemas;
    const keyRef = { $ref: '#/components/schemas/NodeKey' };
    assert.deepEqual(spec.paths['/getAllFunctions'].post.responses['200'].content['application/json'].schema.items, keyRef);
    assert.deepEqual(schemas.CallResult.properties.entry, keyRef);
    assert.deepEqual(schemas.CallResult.properties.functions.items, keyRef);
    assert.deepEqual(schemas.LocationResult.properties.key, keyRef);
    assert.deepEqual(schemas.LocationResult.properties.container.anyOf[0], keyRef);
    assert.equal(schemas.LocationResult.properties.node, undefined);
    assert.deepEqual(schemas.ContentResult.properties.node.anyOf[0], { $ref: '#/components/schemas/Node' });
});

test('class dependencies aggregate member relations and return unique class keys', () => {
    const order = { id: 'order', kind: 'class', name: 'Order', filePath: 'order.py', startLine: 1 };
    const create = { id: 'create', kind: 'method', name: 'create', filePath: 'order.py', startLine: 2 };
    const repo = { id: 'repo', kind: 'class', name: 'Repository', filePath: 'repo.py', startLine: 1 };
    const save = { id: 'save', kind: 'method', name: 'save', filePath: 'repo.py', startLine: 2 };
    const fileOrder = { id: 'file-order', kind: 'file', filePath: 'order.py' };
    const fileRepo = { id: 'file-repo', kind: 'file', filePath: 'repo.py' };
    const nodes = [fileOrder, order, create, fileRepo, repo, save];
    const cg = {
        getNodesByKind: kind => kind === 'file' ? [fileOrder, fileRepo] : [],
        getNodesInFile: path => nodes.filter(node => node.filePath === path),
        getOutgoingEdgesFrom: (ids, kinds) => {
            assert.deepEqual(kinds, ['calls', 'extends', 'implements', 'instantiates']);
            assert.ok(ids.includes(create.id));
            return [
                { source: create.id, target: save.id, kind: 'calls' },
                { source: create.id, target: repo.id, kind: 'instantiates' },
                { source: create.id, target: save.id, kind: 'calls' },
                { source: create.id, target: order.id, kind: 'calls' }
            ];
        },
        getNode: id => nodes.find(node => node.id === id),
        getAncestors: id => ({ create: [order], save: [repo] })[id] || []
    };
    assert.deepEqual(getDependencies(cg, order, 'class'), {
        entry: { name: 'Order', filePath: 'order.py', startLine: 1 },
        dependencies: [{ name: 'Repository', filePath: 'repo.py', startLine: 1 }]
    });
});

test('file dependencies aggregate imports and symbol edges by target path', () => {
    const fileA = { id: 'a', kind: 'file', filePath: 'a.py' };
    const fileB = { id: 'b', kind: 'file', filePath: 'b.py' };
    const importNode = { id: 'import', kind: 'import', filePath: 'a.py' };
    const methodA = { id: 'method-a', kind: 'function', filePath: 'a.py' };
    const methodB = { id: 'method-b', kind: 'function', filePath: 'b.py' };
    const nodes = [fileA, fileB, importNode, methodA, methodB];
    const cg = {
        getNodesInFile: path => nodes.filter(node => node.filePath === path),
        getOutgoingEdgesFrom: (ids, kinds) => {
            assert.deepEqual(kinds, ['calls', 'extends', 'implements', 'instantiates', 'imports']);
            assert.deepEqual(ids, ['a', 'import', 'method-a']);
            return [
                { source: 'import', target: 'b', kind: 'imports' },
                { source: 'method-a', target: 'method-b', kind: 'calls' },
                { source: 'method-a', target: 'method-a', kind: 'calls' }
            ];
        },
        getNode: id => nodes.find(node => node.id === id)
    };
    assert.deepEqual(getDependencies(cg, fileA, 'file'), {
        entry: { kind: 'file', filePath: 'a.py' },
        dependencies: [{ kind: 'file', filePath: 'b.py' }]
    });
});

test('dependency endpoint documents level and target-only results', async () => {
    const app = createHttpApi();
    const spec = await (await app.handle(new Request('http://localhost/doc/json'))).json();
    const operation = spec.paths['/getDependencies'].post;
    assert.deepEqual(operation.requestBody.content['application/json'].schema.properties.level.enum.sort(), ['class', 'file']);
    assert.equal(operation.responses['200'].content['application/json'].schema.$ref, '#/components/schemas/DependencyResult');
    assert.deepEqual(spec.components.schemas.DependencyResult.properties.dependencies.items.anyOf.map(item => item.$ref).sort(), [
        '#/components/schemas/FileSelector', '#/components/schemas/NodeKey'
    ]);
});
