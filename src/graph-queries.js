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

function compareNodes(left, right) {
    return left.filePath.localeCompare(right.filePath)
        || left.startLine - right.startLine
        || left.id.localeCompare(right.id);
}

export function getAllFunctions(cg) {
    return cg.getNodesByKind('function').concat(cg.getNodesByKind('method')).sort(compareNodes);
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
            functions.push(target);
        }
    }
    return { entry: node, functions: functions, edges: edges };
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
