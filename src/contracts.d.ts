import type { Node, Edge, IndexResult, SyncResult } from '@colbymchenry/codegraph';

/** Read a whole file by its project-relative path. */
export interface FileSelector {
    kind: 'file';
    filePath: string;
}

export interface InitializeOptions {
    /** Omit or pass null for all SDK-supported languages. */
    language?: string | string[] | null;
}

export interface CallResult {
    entry: Node;
    /** Direct callees only (functions/methods), excluding entry; each appears once. */
    functions: Node[];
    /** Direct calls edges, including self-recursive calls. */
    edges: Edge[];
}

export interface ContentResult {
    node: Node | null;
    filePath: string;
    source: string;
}

export declare class CodeGraphRepository {
    constructor(projectRoot: string);
    /** Create the index; omit options.language to index all supported languages. */
    initialize(options?: InitializeOptions): Promise<IndexResult | SyncResult>;
    /** Uses the SDK's native sync, preserving the language selection. */
    sync(): Promise<SyncResult>;
    /** node must be a function or method. */
    getCallees(node: Node): Promise<CallResult>;
    /** Pass a Node to read a symbol, or a FileSelector to read a whole file. */
    getContent(node: Node | FileSelector): Promise<ContentResult>;
    /** Returns both function and method nodes. */
    getAllFunctions(): Promise<Node[]>;
}

export function createRepository(projectRoot: string): CodeGraphRepository;
