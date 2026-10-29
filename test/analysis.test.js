import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createRepository } from '../src/index.js';
import { createHttpApi } from '../src/http-api.js';
import { getTestAnalysis, getSourceUnit } from '../src/analysis-queries.js';

test('graph analysis traverses auxiliary cycles without inventing object or fixture facts', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cg-analysis-unit-'));
    try {
        const source = 'def test_total(rate):\n    return helper(rate)\n';
        await fs.writeFile(path.join(root, 'test_total.py'), source);
        const entry = { id: 'test', kind: 'function', name: 'test_total', filePath: 'test_total.py', startLine: 1 };
        const helper = { id: 'helper', kind: 'function', name: 'helper', filePath: 'test_total.py', startLine: 4 };
        const target = { id: 'api', kind: 'method', name: 'total', qualifiedName: 'Order.total', filePath: 'order.py', startLine: 3 };
        const edges = {
            test: [{ kind: 'calls', target: 'helper', line: 2 }, { kind: 'calls', target: 'api' }],
            helper: [{ kind: 'calls', target: 'helper' }, { kind: 'calls', target: 'missing' }]
        };
        const nodes = [entry, helper, target];
        const cg = {
            getNodesByKind: kind => nodes.filter(node => node.kind === kind),
            getNodesInFile: file => nodes.filter(node => node.filePath === file),
            getOutgoingEdges: id => edges[id] || [],
            getNode: id => nodes.find(node => node.id === id)
        };
        const result = await getTestAnalysis(cg, root);
        assert.equal(result.cases.length, 1);
        const item = result.cases[0];
        assert.deepEqual(item.interfaces, ['Order.total']);
        assert.deepEqual(item.dependencies.map(node => node.name), ['helper']);
        assert.equal(item.contents[0].source, source);
        assert.equal(item.facts[0].startLine, 2);
        assert.equal(item.facts[1].startLine, undefined);
        assert.ok(item.facts.every(fact => !('producer' in fact) && !('endLine' in fact)));
        assert.ok(item.warnings.some(warning => warning.includes('fixture')));
        assert.ok(item.warnings.some(warning => warning.includes('missing')));
    } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('actual SDK and HTTP: new APIs, general symbol keys, old content, validation and docs', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cg-analysis-sdk-'));
    try {
        await fs.writeFile(path.join(root, 'order.py'), 'RATE = 2\n\nclass Order:\n    def total(self):\n        return RATE\n');
        await fs.writeFile(path.join(root, 'test_order.py'), 'from order import Order\n\ndef helper():\n    return 2\n\ndef test_total(rate):\n    order = Order()\n    assert order.total() == helper()\n');
        const repository = createRepository(root);
        await repository.initialize({ language: 'python' });
        const app = createHttpApi();
        const call = async (method, body) => app.handle(new Request(`http://localhost/${method}`, {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ projectRoot: root, ...body })
        }));
        const input = { name: 'total', filePath: 'order.py', startLine: 4 };
        const unitResponse = await call('getSourceUnit', { input });
        assert.equal(unitResponse.status, 200);
        const unit = await unitResponse.json();
        assert.deepEqual(unit.entry, input);
        assert.match(unit.contents[0].source, /class Order/);
        assert.match(unit.contents[0].source, /RATE = 2/);
        assert.ok(unit.warnings.length);
        const alias = await call('getSourceUnit', { key: input });
        assert.equal(alias.status, 200);
        const fileResult = await repository.getSourceUnit({ kind: 'file', filePath: 'order.py' });
        assert.equal(fileResult.contents[0].source, unit.contents[0].source);
        const constant = await repository.getSourceUnit({ name: 'RATE', filePath: 'order.py', startLine: 1 });
        assert.equal(constant.entry.name, 'RATE');
        const old = await call('getContent', { input });
        assert.equal(old.status, 200);
        assert.doesNotMatch((await old.json()).source, /class Order/);
        const analysis = await call('getTestAnalysis', {});
        assert.equal(analysis.status, 200);
        const result = await analysis.json();
        assert.deepEqual(result.cases.map(item => item.entry.name), ['test_total']);
        assert.ok(result.cases[0].dependencies.some(node => node.name === 'helper'));
        assert.ok(result.cases[0].contents.some(item => item.source.includes('from order import Order')));
        assert.ok(result.cases[0].warnings.some(value => value.includes('fixture')));
        for (const input of [{ kind: 'file', filePath: '../outside.py' }, { name: 'x', filePath: '../outside.py', startLine: 1 }]) {
            assert.equal((await call('getSourceUnit', { input })).status, 400);
        }
        assert.equal((await call('getSourceUnit', {})).status, 400);
        const spec = await (await app.handle(new Request('http://localhost/doc/json'))).json();
        for (const route of ['getSourceUnit', 'getTestAnalysis', 'getContent']) assert.ok(spec.paths[`/${route}`].post);
        const checkRefs = value => {
            if (!value || typeof value !== 'object') return;
            if (value.$ref) assert.ok(value.$ref.slice(2).split('/').reduce((part, name) => part?.[name], spec), value.$ref);
            Object.values(value).forEach(checkRefs);
        };
        checkRefs(spec);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('empty graph reports incomplete discovery; source failure is not silently swallowed', async () => {
    const result = await getTestAnalysis({ getNodesByKind: () => [] }, '/unused');
    assert.deepEqual(result.cases, []);
    assert.ok(result.warnings.some(warning => warning.includes('未发现')));
    await assert.rejects(getSourceUnit({}, '/unused', { name: 'x', filePath: '/outside', startLine: 1 }), { code: 'INVALID_SELECTOR' });
});
