import assert from 'node:assert/strict';
import test from 'node:test';
import { getAllFunctions, getCallees, getLocation } from '../src/graph-queries.js';
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
