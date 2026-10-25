import fs from 'node:fs/promises';
import path from 'node:path';

const SYMBOL_KINDS = ['function', 'method', 'class', 'struct', 'interface'];

function selectorError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
}

function keyOf(key) {
    if (!key || typeof key.name !== 'string' || key.name.trim().length === 0 ||
        typeof key.filePath !== 'string' || key.filePath.trim().length === 0 ||
        !Number.isInteger(key.startLine) || key.startLine < 1) {
        throw selectorError('INVALID_SELECTOR', 'Expected { name, filePath, startLine }; startLine must be a positive integer.');
    }
    const filePath = path.posix.normalize(key.filePath.replace(/\\/g, '/'));
    if (path.posix.isAbsolute(filePath) || path.win32.isAbsolute(filePath) ||
        filePath === '.' || filePath === '..' || filePath.startsWith('../')) {
        throw selectorError('INVALID_SELECTOR', 'filePath must be relative to the repository root.');
    }
    return JSON.stringify([key.name, filePath, key.startLine]);
}

function buildIndex(nodes) {
    if (!Array.isArray(nodes)) {
        throw new Error('Invalid node cache.');
    }
    const index = new Map();
    for (const node of nodes) {
        if (!node || !SYMBOL_KINDS.includes(node.kind) || typeof node.id !== 'string' || !node.id) {
            throw new Error('Invalid cached node.');
        }
        const key = keyOf(node);
        const matches = index.get(key) || [];
        matches.push(node);
        index.set(key, matches);
    }
    return index;
}

function loadSymbolNodes(cg) {
    return SYMBOL_KINDS.flatMap((kind) => cg.getNodesByKind(kind));
}

/** Internal cache; not part of the package's public API. */
export class NodeCache {
    #index = null;
    #loading = null;

    constructor(projectRoot) {
        this.filePath = path.join(projectRoot, '.codegraph/nodes-cache.json');
    }

    clearMemory() {
        this.#index = null;
    }

    async invalidate() {
        this.clearMemory();
        await fs.rm(this.filePath, { force: true });
    }

    async refreshNodeCache(cg) {
        this.clearMemory();
        const temporaryPath = this.filePath + '.tmp';
        try {
            const nodes = loadSymbolNodes(cg);
            const index = buildIndex(nodes);
            await fs.writeFile(temporaryPath, JSON.stringify({ version: 1, nodes }) + '\n');
            await fs.rename(temporaryPath, this.filePath);
            this.#index = index;
        } catch (error) {
            await this.invalidate();
            throw error;
        } finally {
            await fs.rm(temporaryPath, { force: true });
        }
    }

    async #load(cg) {
        let index;
        try {
            const cache = JSON.parse(await fs.readFile(this.filePath, 'utf8'));
            if (cache.version !== 1) {
                throw new Error('Unsupported node cache version.');
            }
            index = buildIndex(cache.nodes);
        } catch (error) {
            if (error.code && error.code !== 'ENOENT' && error.code !== 'INVALID_SELECTOR') {
                throw error;
            }
            await this.refreshNodeCache(cg);
            return;
        }
        this.#index = index;
    }

    async findNode(key, cg) {
        const lookup = keyOf(key);
        if (this.#index === null) {
            if (this.#loading === null) {
                this.#loading = this.#load(cg).finally(() => { this.#loading = null; });
            }
            await this.#loading;
        }
        const matches = this.#index.get(lookup) || [];
        if (matches.length === 0) {
            throw selectorError('NODE_NOT_FOUND', 'No node matches the supplied key.');
        }
        if (matches.length > 1) {
            const error = selectorError('AMBIGUOUS_NODE', 'Multiple nodes match the supplied key; inspect error.candidates.');
            error.candidates = matches;
            throw error;
        }
        return matches[0];
    }
}
