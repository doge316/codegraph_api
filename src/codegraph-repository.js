import fs from 'node:fs/promises';
import path from 'node:path';
import codegraph from '@colbymchenry/codegraph';
import { normalizeLanguages, collectLanguageFiles } from './language-scope.js';
import { getCallees, getContent, getAllFunctions, getLocation, getDependencies } from './graph-queries.js';
import { NodeCache } from './node-cache.js';

const POLICY_PATH = '.codegraph/repository-api.json';

/** Repository analyzer backed by the official CodeGraph SDK. */
export class CodeGraphRepository {
    #nodeCache;

    constructor(projectRoot) {
        if (typeof projectRoot !== 'string' || projectRoot.length === 0) {
            throw new Error('projectRoot must be a non-empty path.');
        }
        this.projectRoot = path.resolve(projectRoot);
        this.#nodeCache = new NodeCache(this.projectRoot);
    }

    async initialize(options) {
        const languages = normalizeLanguages(options && options.language, codegraph);
        const stat = await fs.stat(this.projectRoot);
        if (!stat.isDirectory()) {
            throw new Error('projectRoot must be an existing directory.');
        }
        const cg = await codegraph.CodeGraph.init(this.projectRoot, { index: false });
        this.#nodeCache.clearMemory();
        try {
            await fs.writeFile(path.join(this.projectRoot, POLICY_PATH), JSON.stringify({
                version: 1,
                languages: languages
            }, null, 2) + '\n');
            let result;
            if (languages === null) {
                result = await cg.indexAll();
                if (!result.success || result.filesErrored > 0) {
                    const error = new Error('CodeGraph indexing failed; inspect error.result, then retry sync().');
                    error.result = result;
                    throw error;
                }
            } else {
                result = await this.syncSelectedLanguages(cg, languages);
            }
            await this.#nodeCache.refreshNodeCache(cg);
            return result;
        } catch (error) {
            await this.#nodeCache.invalidate();
            throw error;
        } finally {
            cg.close();
        }
    }

    async readLanguages() {
        try {
            const content = await fs.readFile(path.join(this.projectRoot, POLICY_PATH), 'utf8');
            const policy = JSON.parse(content);
            if (policy.version !== 1 || !Object.prototype.hasOwnProperty.call(policy, 'languages')) {
                throw new Error('Invalid repository-api.json language policy.');
            }
            return normalizeLanguages(policy.languages, codegraph);
        } catch (error) {
            if (error.code === 'ENOENT') {
                return null;
            }
            throw error;
        }
    }

    async withGraph(operation, readOnly) {
        const cg = await codegraph.CodeGraph.open(this.projectRoot, { readOnly: readOnly === true });
        try {
            return await operation(cg);
        } finally {
            cg.close();
        }
    }

    async syncSelectedLanguages(cg, languages) {
        const currentFiles = await collectLanguageFiles(this.projectRoot, languages, codegraph);
        const paths = new Set(currentFiles);
        // Include old paths so native sync detects removed and renamed files.
        for (const file of cg.getFiles()) {
            if (!languages.includes(file.language)) {
                throw new Error('The database contains unselected languages. Rebuild it before using this language scope.');
            }
            paths.add(file.path);
        }
        // Empty paths means FULL sync in CodeGraph. An ignored metadata path
        // makes an empty selected repository a safe, scoped no-op instead.
        if (paths.size === 0) {
            paths.add(POLICY_PATH);
        }
        return cg.sync({ paths: Array.from(paths).sort() });
    }

    async sync() {
        const languages = await this.readLanguages();
        return this.withGraph(async (cg) => {
            this.#nodeCache.clearMemory();
            try {
                const result = languages === null
                    ? await cg.sync()
                    : await this.syncSelectedLanguages(cg, languages);
                await this.#nodeCache.refreshNodeCache(cg);
                return result;
            } catch (error) {
                await this.#nodeCache.invalidate();
                throw error;
            }
        });
    }

    async getCallees(key) {
        return this.withGraph(async (cg) => getCallees(cg, await this.#nodeCache.findNode(key, cg)), true);
    }

    async getContent(input) {
        return this.withGraph(async (cg) => {
            const node = input && input.kind === 'file'
                ? input
                : await this.#nodeCache.findNode(input, cg);
            return getContent(cg, this.projectRoot, node);
        }, true);
    }

    async getAllFunctions() {
        return this.withGraph((cg) => getAllFunctions(cg), true);
    }

    async getLocation(key) {
        return this.withGraph(async (cg) => getLocation(cg, await this.#nodeCache.findNode(key, cg)), true);
    }

    async getDependencies(level, input) {
        return this.withGraph(async (cg) => {
            if (level === 'class') {
                const node = await this.#nodeCache.findNode(input, cg);
                return getDependencies(cg, node, level);
            }
            if (level === 'file') {
                if (!input || input.kind !== 'file' || typeof input.filePath !== 'string' || !input.filePath.trim()) {
                    const error = new Error('File dependency queries require { kind: "file", filePath }.');
                    error.code = 'INVALID_SELECTOR';
                    throw error;
                }
                const filePath = path.posix.normalize(input.filePath.replace(/\\/g, '/'));
                if (path.posix.isAbsolute(filePath) || path.win32.isAbsolute(filePath) || filePath === '..' || filePath.startsWith('../')) {
                    const error = new Error('filePath must stay inside the project root.');
                    error.code = 'INVALID_SELECTOR';
                    throw error;
                }
                const root = await fs.realpath(this.projectRoot);
                const absolutePath = await fs.realpath(path.join(root, filePath));
                const relativePath = path.relative(root, absolutePath);
                if (relativePath.startsWith('..' + path.sep) || path.isAbsolute(relativePath) || !(await fs.stat(absolutePath)).isFile()) {
                    const error = new Error('filePath must reference a file inside the project root.');
                    error.code = 'INVALID_SELECTOR';
                    throw error;
                }
                const nodes = cg.getNodesInFile(filePath);
                const entry = nodes.find((node) => node.kind === 'file') || { id: filePath, kind: 'file', filePath };
                return getDependencies(cg, entry, level);
            }
            return getDependencies(cg, null, level);
        }, true);
    }
}
