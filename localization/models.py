"""解析器共享的数据记录；对外接口返回普通字典，不维护序列化协议。"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal
import hashlib
import json

SourceLanguage = str
UnitKind = str
DependencyKind = str
Origin = str

class Model:
    def to_dict(self):
        return asdict(self)
    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

@dataclass(frozen=True)
class Location(Model):
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int

@dataclass(frozen=True)
class Diagnostic(Model):
    code: str
    message: str
    location: Location | None = None
    blocking: bool = True
    related_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

@dataclass(frozen=True)
class PublicFile(Model):
    relative_path: str
    content_bytes: bytes
    sha256: str
    role: Literal['test', 'helper', 'resource']

@dataclass(frozen=True)
class PublicBundle(Model):
    root: Path
    files: tuple[PublicFile, ...]
    input_hash: str

@dataclass(frozen=True)
class GraphNode(Model):
    id: str
    name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    qualified_name: str | None = None

@dataclass(frozen=True)
class GraphCall(Model):
    source_id: str
    target_id: str

@dataclass(frozen=True)
class GraphContent(Model):
    file_path: str
    source: str

@dataclass(frozen=True)
class TestGraphSnapshot(Model):
    __test__ = False
    input_hash: str
    nodes: tuple[GraphNode, ...] = ()
    calls: tuple[GraphCall, ...] = ()
    contents: tuple[GraphContent, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

@dataclass(frozen=True)
class Evidence(Model):
    evidence_id: str
    location: Location
    kind: str
    fact: dict[str, Any]
    origin: Literal['ast', 'codegraph', 'llm'] = 'ast'

@dataclass(frozen=True)
class InterfaceRef(Model):
    interface_id: str
    spelling: str
    receiver_origin: str | None
    role: Literal['construct', 'prepare', 'asserted', 'cleanup', 'unknown']
    evidence_ids: tuple[str, ...]
    resolution: Literal['resolved', 'unknown'] = 'resolved'

@dataclass(frozen=True)
class SupportUnit(Model):
    unit_id: str
    name: str
    container: str
    location: Location
    kind: Literal['helper', 'fixture', 'context']
    dependencies: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()

@dataclass(frozen=True)
class TestCase(Model):
    __test__ = False
    case_id: str
    file_path: str
    container: str
    name: str
    location: Location
    parameterization: Literal['none', 'static', 'dynamic']
    asserted_interfaces: tuple[str, ...]
    requirements: tuple[InterfaceRef, ...]
    support_ids: tuple[str, ...]
    runtime_selector: str
    fixture_names: tuple[str, ...] = ()

@dataclass(frozen=True)
class UnresolvedItem(Model):
    item_id: str
    case_ids: tuple[str, ...]
    reason: str
    evidence_ids: tuple[str, ...]
    can_ask_llm: bool

@dataclass(frozen=True)
class ParsedTests(Model):
    __test__ = False
    framework: str
    cases: tuple[TestCase, ...]
    support_units: tuple[SupportUnit, ...]
    syntax_facts: tuple[Evidence, ...]
    unresolved: tuple[UnresolvedItem, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

@dataclass(frozen=True)
class TestInventory(Model):
    __test__ = False
    input_hash: str
    framework: str
    cases: tuple[TestCase, ...]
    support_units: tuple[SupportUnit, ...]
    facts: tuple[Evidence, ...]
    unresolved: tuple[UnresolvedItem, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    snippets: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class Feature(Model):
    feature_id: str
    name: str
    interface_ids: tuple[str, ...]

@dataclass(frozen=True)
class Assignment(Model):
    case_id: str
    feature_ids: tuple[str, ...]

@dataclass(frozen=True)
class Relation(Model):
    before_feature: str
    after_feature: str
    basis: Literal['test_fact', 'semantic_candidate']
    evidence_ids: tuple[str, ...]

@dataclass(frozen=True)
class AtomicConstraint(Model):
    case_ids: tuple[str, ...]
    reason: str
    ordered: bool = False

@dataclass(frozen=True)
class CandidatePlan(Model):
    features: tuple[Feature, ...]
    assignments: tuple[Assignment, ...]
    relations: tuple[Relation, ...]
    atomic_constraints: tuple[AtomicConstraint, ...]
    unresolved: tuple[UnresolvedItem, ...]
    evidence: tuple[Evidence, ...]

@dataclass(frozen=True)
class SourceSymbol(Model):
    symbol_id: str
    graph_node_id: str | None
    name: str
    qualified_name: str
    kind: str
    signature: str | None
    owner_id: str | None
    locations: tuple[Location, ...]

@dataclass(frozen=True)
class SourceFragment(Model):
    location: Location
    source: str
    file_sha256: str

@dataclass(frozen=True)
class SourceUnit(Model):
    unit_id: str
    kind: UnitKind
    language: SourceLanguage
    qualified_name: str
    member_symbol_ids: tuple[str, ...]
    fragments: tuple[SourceFragment, ...]
    imports: tuple[str, ...]
    reason: str

@dataclass(frozen=True)
class DependencyEdge(Model):
    source_unit_id: str
    target_unit_id: str
    kind: DependencyKind
    evidence: Location
    origin: Origin

@dataclass(frozen=True)
class ExternalDependency(Model):
    name: str
    version: str | None
    evidence: Location

@dataclass(frozen=True)
class ResourceReference(Model):
    path: str | None
    expression: str
    exists: bool | None
    evidence: Location

@dataclass(frozen=True)
class Reference(Model):
    spelling: str
    kind: str
    scope_id: str | None
    location: Location

@dataclass(frozen=True)
class ParsedFile(Model):
    file_path: str
    sha256: str
    symbols: tuple[SourceSymbol, ...]
    imports: tuple[str, ...]
    references: tuple[Reference, ...]
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class SourceInventory(Model):
    repo_root: Path
    language: SourceLanguage
    files: dict[str, ParsedFile]
    symbols: dict[str, SourceSymbol]
    graph_bindings: dict[str, str]
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class InternalDependency(Model):
    target: SourceSymbol
    kind: DependencyKind
    evidence: Location
    origin: Origin

@dataclass(frozen=True)
class DependencyScan(Model):
    internal_dependencies: tuple[InternalDependency, ...]
    external_dependencies: tuple[ExternalDependency, ...]
    resources: tuple[ResourceReference, ...]
    diagnostics: tuple[Diagnostic, ...]
