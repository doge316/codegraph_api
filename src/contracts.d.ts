import type { Node, Edge, IndexResult, SyncResult } from '@colbymchenry/codegraph';

/** Read a whole file by its project-relative path. */
export interface FileSelector {
    kind: 'file';
    filePath: string;
}

/** Exact symbol lookup; paths are project-relative and lines are 1-indexed. */
export interface NodeKey {
    name: string;
    filePath: string;
    startLine: number;
}

export interface InitializeOptions {
    /** Omit or pass null for all SDK-supported languages. */
    language?: string | string[] | null;
}

export interface CallResult {
    entry: NodeKey;
    /** Direct callees only (functions/methods), excluding entry; each appears once. */
    functions: NodeKey[];
    /** Direct calls edges, including self-recursive calls. */
    edges: Edge[];
}

export interface ContentResult {
    node: Node | null;
    filePath: string;
    source: string;
}

export interface LocationResult {
    key: NodeKey;
    filePath: string;
    /** Nearest enclosing class, struct, or interface recorded by the SDK; null if none is found. */
    container: NodeKey | null;
}

export interface DependencyResult<TEntry, TDependency> {
    entry: TEntry;
    dependencies: TDependency[];
}

export interface SourceUnitResult {
    entry: NodeKey | FileSelector;
    /** Conservative whole-file context; may contain unrelated declarations. */
    contents: ContentResult[];
    dependencies: NodeKey[];
    warnings: string[];
}

/** Graph evidence only; no inferred object producer, arguments or expression end lines. */
export interface TestGraphFact {
    kind: Edge['kind'];
    owner: NodeKey;
    target: NodeKey | null;
    consumer: string;
    filePath: string;
    startLine?: number;
}

export interface TestCaseAnalysis {
    entry: NodeKey;
    interfaces: string[];
    dependencies: NodeKey[];
    contents: ContentResult[];
    facts: TestGraphFact[];
    warnings: string[];
}

export interface TestAnalysisResult {
    cases: TestCaseAnalysis[];
    warnings: string[];
}

export declare class CodeGraphRepository {
    constructor(projectRoot: string);
    /** Create the index; omit options.language to index all supported languages. */
    initialize(options?: InitializeOptions): Promise<IndexResult | SyncResult>;
    /** Uses the SDK's native sync, preserving the language selection. */
    sync(): Promise<SyncResult>;
    /** The key must identify exactly one function or method. */
    getCallees(key: NodeKey): Promise<CallResult>;
    /** Pass a NodeKey to read a symbol, or a FileSelector to read a whole file. */
    getContent(input: NodeKey | FileSelector): Promise<ContentResult>;
    /** CodeGraph-only analysis; requires an initialized index. Inspect warnings. */
    getSourceUnit(input: NodeKey | FileSelector): Promise<SourceUnitResult>;
    /** Naming/decorator-based candidates and resolved graph evidence, not framework collection. */
    getTestAnalysis(): Promise<TestAnalysisResult>;
    /** Returns keys for both functions and methods. */
    getAllFunctions(): Promise<NodeKey[]>;
    /** Find the file and nearest enclosing type of exactly one function or method. */
    getLocation(key: NodeKey): Promise<LocationResult>;
    /** Return direct class-to-class or file-to-file dependencies, deduplicated by target. */
    getDependencies(level: 'class', input: NodeKey): Promise<DependencyResult<NodeKey, NodeKey>>;
    getDependencies(level: 'file', input: FileSelector): Promise<DependencyResult<FileSelector, FileSelector>>;
}

export function createRepository(projectRoot: string): CodeGraphRepository;
