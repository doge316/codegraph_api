import { CodeGraphRepository } from './codegraph-repository.js';

/**
 * Create a repository analyzer for the project at `projectRoot`.
 * @param {string} projectRoot
 * @returns {import('./codegraph-repository.js').CodeGraphRepository}
 */
export function createRepository(projectRoot) {
    return new CodeGraphRepository(projectRoot);
}

export { CodeGraphRepository };
