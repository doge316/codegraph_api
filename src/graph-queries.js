import fs from 'node:fs/promises';
import path from 'node:path';

function apiError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
}

function normalizeFilePath(filePath) {
    if (typeof filePath !== 'string' || filePath.length === 0) {
        throw apiError('INVALID_SELECTOR', 'filePath must be a non-empty project-relative path.');
    }
    const normalized = path.posix.normalize(filePath.replace(/\\/g, '/'));
    if (path.posix.isAbsolute(normalized) || normalized === '..' || normalized.startsWith('../')) {
        throw apiError('INVALID_SELECTOR', 'filePath must stay inside the project root.');
    }
    return normalized;
}

function isFunction(node) {
    return node && (node.kind === 'function' || node.kind === 'method');
}

function toKey(node) {
    return { name: node.name, filePath: node.filePath, startLine: node.startLine };
}

const TYPE_KINDS = new Set(['class', 'struct', 'interface']);
const CLASS_RELATIONS = ['calls', 'extends', 'implements', 'instantiates'];
const FILE_RELATIONS = [...CLASS_RELATIONS, 'imports'];

function nearestType(cg, node) {
    if (TYPE_KINDS.has(node.kind)) return node;
    return cg.getAncestors(node.id).find((ancestor) => TYPE_KINDS.has(ancestor.kind)) || null;
}

export function getDependencies(cg, entry, level) {
    if (!['class', 'file'].includes(level)) {
        throw apiError('INVALID_SELECTOR', 'level must be class or file.');
    }

    if (level === 'class' && !TYPE_KINDS.has(entry.kind)) {
        throw apiError('INVALID_SELECTOR', 'Class dependency queries require a class, struct, or interface key.');
    }
    if (level === 'file' && entry.kind !== 'file') {
        throw apiError('INVALID_SELECTOR', 'File dependency queries require a file selector.');
    }

    const nodes = cg.getNodesInFile(entry.filePath);
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const edges = cg.getOutgoingEdgesFrom(nodes.map((node) => node.id),
        level === 'class' ? CLASS_RELATIONS : FILE_RELATIONS);
    const targets = new Map();
    const typeByNode = new Map();
    const getType = (node) => {
        if (!typeByNode.has(node.id)) typeByNode.set(node.id, nearestType(cg, node));
        return typeByNode.get(node.id);
    };

    for (const edge of edges) {
        const source = byId.get(edge.source);
        const target = cg.getNode(edge.target);
        if (!source || !target) continue;

        const sourceGroup = level === 'class' ? getType(source)?.id : source.filePath;
        if (sourceGroup !== (level === 'class' ? entry.id : entry.filePath)) continue;

        const targetGroup = level === 'class' ? getType(target) : target;
        if (!targetGroup) continue;
        if (level === 'file' && (typeof targetGroup.filePath !== 'string' || !targetGroup.filePath)) continue;
        const targetIdentity = level === 'class' ? targetGroup.id : targetGroup.filePath;
        if (targetIdentity === (level === 'class' ? entry.id : entry.filePath)) continue;
        if (!targets.has(targetIdentity)) {
            targets.set(targetIdentity, level === 'class'
                ? toKey(targetGroup)
                : { kind: 'file', filePath: targetGroup.filePath });
        }
    }

    const resultEntry = level === 'class'
        ? toKey(entry)
        : { kind: 'file', filePath: entry.filePath };
    return { entry: resultEntry, dependencies: [...targets.values()] };
}

function compareNodes(left, right) {
    return left.filePath.localeCompare(right.filePath)
        || left.startLine - right.startLine
        || left.id.localeCompare(right.id);
}

export function getLocation(cg, node) {
    if (!isFunction(node)) {
        throw apiError('INVALID_SELECTOR', 'Location queries accept functions or methods.');
    }
    const container = cg.getAncestors(node.id).find((ancestor) =>
        ancestor.kind === 'class' || ancestor.kind === 'struct' || ancestor.kind === 'interface'
    ) || null;
    return { key: toKey(node), filePath: node.filePath, container: container ? toKey(container) : null };
}

export function getAllFunctions(cg) {
    return cg.getNodesByKind('function').concat(cg.getNodesByKind('method')).sort(compareNodes).map(toKey);
}

export function getCallees(cg, node) {
    if (!isFunction(node)) {
        throw apiError('INVALID_SELECTOR', 'The call entry must be a function or method.');
    }
    const functions = [];
    const edges = [];
    const seen = new Set([node.id]);
    for (const edge of cg.getOutgoingEdges(node.id)) {
        // Keep raw calls edges, including self-recursion; the entry itself is excluded.
        if (edge.kind !== 'calls') {
            continue;
        }
        edges.push(edge);
        const target = cg.getNode(edge.target);
        if (!target || seen.has(target.id)) {
            continue;
        }
        seen.add(target.id);
        if (isFunction(target)) {
            functions.push(toKey(target));
        }
    }
    return { entry: toKey(node), functions: functions, edges: edges };
}

export async function getContent(cg, projectRoot, input) {
    if (input && input.kind === 'file' && input.filePath) {
        const filePath = normalizeFilePath(input.filePath);
        const root = await fs.realpath(projectRoot);
        const absolutePath = await fs.realpath(path.join(root, filePath));
        const relativePath = path.relative(root, absolutePath);
        if (relativePath.startsWith('..' + path.sep) || path.isAbsolute(relativePath)) {
            throw apiError('INVALID_SELECTOR', 'The file resolves outside the project root.');
        }
        const fileNodes = cg.getNodesInFile(filePath);
        const fileNode = fileNodes.find(function (node) { return node.kind === 'file'; }) || null;
        return { node: fileNode, filePath: filePath, source: await fs.readFile(absolutePath, 'utf8') };
    }
    const node = input;
    if (!isFunction(node) && node.kind !== 'class' && node.kind !== 'struct' && node.kind !== 'interface') {
        throw apiError('INVALID_SELECTOR', 'Content queries accept functions, methods, classes, structs, interfaces, or files.');
    }
    const source = await cg.getCode(node.id);
    if (source === null || source === undefined) {
        throw apiError('SOURCE_NOT_FOUND', 'The indexed symbol source is unavailable; sync the repository.');
    }
    return { node: node, filePath: node.filePath, source: source };
}
