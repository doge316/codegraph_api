import { getContent } from './graph-queries.js';

const types = new Set(['class', 'struct', 'interface']);
const relations = new Set(['calls', 'extends', 'implements', 'instantiates', 'references', 'type_of', 'returns', 'imports']);
const key = (node) => ({ name: node.name, filePath: node.filePath, startLine: node.startLine });
const compare = (a, b) => a.filePath.localeCompare(b.filePath) || a.startLine - b.startLine || a.name.localeCompare(b.name);
const hasKey = (node) => node && typeof node.name === 'string' && node.name.length > 0 &&
    typeof node.filePath === 'string' && node.filePath.length > 0 && Number.isInteger(node.startLine) && node.startLine > 0;
const baseline = '仅基于 CodeGraph 当前索引；缺失边不代表没有依赖，源码修改后请先 sync。';

function error(code, message) {
    return Object.assign(new Error(message), { code });
}

// This locator also accepts constants and aliases outside the five-kind node cache.
export function findAnalysisNode(cg, input) {
    if (!hasKey(input)) throw error('INVALID_SELECTOR', 'Expected { name, filePath, startLine }.');
    const path = input.filePath.replace(/\\/g, '/');
    // Use repository-relative paths only; normalize without interpreting source text.
    const parts = [];
    if (path.startsWith('/') || /^[A-Za-z]:/.test(path)) throw error('INVALID_SELECTOR', 'filePath must be project-relative.');
    for (const part of path.split('/')) {
        if (part === '..') {
            if (!parts.length) throw error('INVALID_SELECTOR', 'filePath must stay inside the project root.');
            parts.pop();
        } else if (part && part !== '.') parts.push(part);
    }
    if (!parts.length) throw error('INVALID_SELECTOR', 'Expected a file path.');
    const matches = cg.getNodesInFile(parts.join('/')).filter(node => node.name === input.name && node.startLine === input.startLine);
    if (!matches.length) throw error('NODE_NOT_FOUND', 'No node matches the supplied key.');
    if (matches.length > 1) throw Object.assign(error('AMBIGUOUS_NODE', 'Multiple nodes match the supplied key.'), { candidates: matches });
    return matches[0];
}

function outgoing(cg, nodes, warnings) {
    const result = [];
    for (const node of nodes) {
        for (const edge of cg.getOutgoingEdges(node.id)) {
            if (!relations.has(edge.kind)) continue;
            const target = cg.getNode(edge.target);
            if (!target) {
                warnings.add(`未解析目标节点：${edge.kind} ${edge.target}`);
                continue;
            }
            result.push({ source: node, target, edge });
        }
    }
    return result;
}

async function filesContent(cg, root, paths) {
    return Promise.all([...paths].sort().map(filePath => getContent(cg, root, { kind: 'file', filePath })));
}

export async function getSourceUnit(cg, root, input) {
    const warnings = new Set([baseline,
        '上下文采用完整所在文件保守保留，可能包含无关代码；不保证跨文件类外实现、partial 或动态上下文完整。']);
    let entry;
    let nodes;
    let filePath;
    if (input?.kind === 'file') {
        const content = await getContent(cg, root, input);
        filePath = content.filePath;
        entry = { kind: 'file', filePath };
        nodes = cg.getNodesInFile(filePath);
    } else {
        const node = findAnalysisNode(cg, input);
        const unit = types.has(node.kind) ? node :
            cg.getAncestors(node.id).find(ancestor => types.has(ancestor.kind)) || node;
        entry = key(node);
        filePath = unit.filePath;
        // Full file preserves imports, decorators and globals without an extra parser.
        nodes = cg.getNodesInFile(filePath);
    }
    const dependencies = new Map();
    for (const { target } of outgoing(cg, nodes, warnings)) {
        if (target.filePath === filePath) continue; // Already included in contents.
        if (target.kind === 'file' || !hasKey(target)) {
            warnings.add(`依赖没有可继续查询的符号 key：${target.filePath || target.id}`);
            continue;
        }
        dependencies.set(JSON.stringify(key(target)), key(target));
    }
    return { entry, contents: await filesContent(cg, root, new Set([filePath])),
        dependencies: [...dependencies.values()].sort(compare), warnings: [...warnings] };
}

function testCandidate(node) {
    const decorators = node.decorators || [];
    return /^test(?:_|[A-Z]|$)/.test(node.name) || decorators.some(value =>
        /(?:^|[.@])(?:Test|ParameterizedTest|RepeatedTest|Theory|Fact|test)(?:\b|\()/.test(value));
}

export async function getTestAnalysis(cg, root) {
    const functions = cg.getNodesByKind('function').concat(cg.getNodesByKind('method'));
    const candidates = functions.filter(testCandidate).sort(compare);
    const candidateIds = new Set(candidates.map(node => node.id));
    const testFiles = new Set(candidates.map(node => node.filePath));
    const support = (node) => testFiles.has(node.filePath) ||
        /(?:^|\/)(?:conftest|helpers?|fixtures?)\.[^/]+$/.test(node.filePath) ||
        /(?:^|\/)(?:tests?|helpers?|fixtures?)\//.test(node.filePath);
    const cases = [];
    for (const entry of candidates) {
        const warnings = new Set([baseline,
            '测试身份按节点名称或装饰器筛选，未验证框架收集规则；helper 与被测接口按文件位置启发式区分。',
            '无法确认隐式 fixture、autouse、参数化及生命周期依赖；未执行对象来源、接收者或传参数据流分析。',
            'contents 保留测试与已解析辅助实体所在文件的完整原文，不能保证可直接独立组装运行。']);
        const dependencies = new Map();
        const interfaces = new Set();
        const facts = [];
        const paths = new Set([entry.filePath]);
        const visited = new Set();
        const queue = [entry];
        for (let i = 0; i < queue.length; i++) {
            const node = queue[i];
            if (visited.has(node.id)) continue;
            visited.add(node.id);
            for (const { target, edge } of outgoing(cg, [node], warnings)) {
                const auxiliary = support(target);
                if (node.id === entry.id && ['calls', 'instantiates'].includes(edge.kind) && !auxiliary) {
                    interfaces.add(target.qualifiedName || target.name);
                }
                const fact = { kind: edge.kind, owner: key(node), target: hasKey(target) ? key(target) : null,
                    consumer: target.qualifiedName || target.name, filePath: node.filePath };
                if (Number.isInteger(edge.line) && edge.line > 0) fact.startLine = edge.line;
                facts.push(fact); // SDK edges do not establish expression end lines or argument flow.
                if (!auxiliary || target.kind === 'file' || !hasKey(target) || candidateIds.has(target.id)) continue;
                dependencies.set(JSON.stringify(key(target)), key(target));
                paths.add(target.filePath);
                queue.push(target);
            }
        }
        cases.push({ entry: key(entry), interfaces: [...interfaces].sort(),
            dependencies: [...dependencies.values()].sort(compare), contents: await filesContent(cg, root, paths),
            facts, warnings: [...warnings] });
    }
    return { cases, warnings: [baseline, '只返回按名称或装饰器识别的测试候选，可能遗漏框架动态收集的测试。',
        ...(!cases.length ? ['当前索引中未发现符合命名或装饰器规则的测试候选。'] : [])] };
}
