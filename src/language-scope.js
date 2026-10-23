import fs from 'node:fs/promises';
import path from 'node:path';

// These are generated/dependency directories, not repository source inputs.
const SKIP_DIRECTORIES = new Set([
    '.git', '.codegraph', 'node_modules', '.venv', 'venv', '__pycache__'
]);

export function normalizeLanguages(language, codegraph) {
    if (language === undefined || language === null) {
        return null;
    }
    const values = Array.isArray(language) ? language : [language];
    if (values.length === 0) {
        throw new Error('language must be null, a language name, or a non-empty array.');
    }
    const supported = codegraph.getSupportedLanguages();
    const languages = [];
    for (const value of values) {
        if (typeof value !== 'string' || !supported.includes(value)) {
            throw new Error('Unsupported language: ' + String(value));
        }
        if (!languages.includes(value)) {
            languages.push(value);
        }
    }
    return languages;
}

/** Discover candidates; SDK sync still applies its gitignore/exclude scope. */
export async function collectLanguageFiles(projectRoot, languages, codegraph) {
    const selected = [];
    let overrides = {};
    try {
        const config = JSON.parse(await fs.readFile(path.join(projectRoot, 'codegraph.json'), 'utf8'));
        overrides = config.extensions || {};
    } catch (error) {
        if (error.code !== 'ENOENT') {
            throw error;
        }
    }
    const pending = [''];
    while (pending.length > 0) {
        const directory = pending.pop();
        const entries = await fs.readdir(path.join(projectRoot, directory), { withFileTypes: true });
        for (const entry of entries) {
            const relativePath = directory ? directory + '/' + entry.name : entry.name;
            if (entry.isDirectory() && !SKIP_DIRECTORIES.has(entry.name)) {
                pending.push(relativePath);
            } else if (entry.isFile()) {
                const language = codegraph.detectLanguage(relativePath, undefined, overrides, projectRoot);
                if (languages.includes(language)) {
                    selected.push(relativePath);
                }
            }
        }
    }
    return selected.sort();
}
