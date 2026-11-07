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
class SupportUnit(Model):
    unit_id: tuple[str, str, int]
    name: str
    container: str
    location: Location
    kind: Literal['helper', 'fixture', 'context']
    dependencies: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()

@dataclass(frozen=True)
class TestCase(Model):
    __test__ = False
    case_id: tuple[str, str, int]
    file_path: str
    container: str
    name: str
    location: Location
    parameterization: Literal['none', 'static', 'dynamic']
    requirements: tuple[str, ...]
    support_ids: tuple[tuple[str, str, int], ...]
    runtime_selector: str

@dataclass(frozen=True)
class ParsedTests(Model):
    __test__ = False
    framework: str
    cases: tuple[TestCase, ...]
    support_units: tuple[SupportUnit, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

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
