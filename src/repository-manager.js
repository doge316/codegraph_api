import fs from 'node:fs/promises';
import path from 'node:path';
import { createRepository } from './index.js';

/** Keeps repository instances and their memory caches alive between requests. */
export class RepositoryManager {
    #repositories = new Map();

    async getRepository(projectRoot) {
        if (typeof projectRoot !== 'string' || !projectRoot.trim() || !path.isAbsolute(projectRoot)) {
            const error = new Error('projectRoot must be an absolute path on the server.');
            error.code = 'INVALID_SELECTOR';
            throw error;
        }
        const root = await fs.realpath(projectRoot);
        if (!(await fs.stat(root)).isDirectory()) {
            const error = new Error('projectRoot must be an existing directory.');
            error.code = 'INVALID_SELECTOR';
            throw error;
        }
        if (!this.#repositories.has(root)) {
            this.#repositories.set(root, createRepository(root));
        }
        return this.#repositories.get(root);
    }
}
