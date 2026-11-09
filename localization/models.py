"""解析器共享的数据记录；对外接口返回普通字典，不维护序列化协议。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import hashlib
import json

SourceLanguage = str
UnitKind = str
DependencyKind = str
Origin = str

def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

@dataclass(frozen=True)
class Location:
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int

@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    location: Location | None = None
    blocking: bool = True
    related_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

@dataclass(frozen=True)
class PublicFile:
    relative_path: str
    content_bytes: bytes
    sha256: str
    role: Literal['test', 'helper', 'resource']

@dataclass(frozen=True)
class PublicBundle:
    root: Path
    files: tuple[PublicFile, ...]
    input_hash: str

@dataclass(frozen=True)
class SupportUnit:
    unit_id: tuple[str, str, int]
    name: str
    container: str
    location: Location
    kind: Literal['helper', 'fixture', 'context']
    dependencies: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()

@dataclass(frozen=True)
class TestCase:
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
class ParsedTests:
    __test__ = False
    framework: str
    cases: tuple[TestCase, ...]
    support_units: tuple[SupportUnit, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

@dataclass(frozen=True)
class SourceSymbol:
    symbol_id: str
    graph_node_id: str | None
    name: str
    qualified_name: str
    kind: str
    signature: str | None
    owner_id: str | None
    locations: tuple[Location, ...]

@dataclass(frozen=True)
class SourceFragment:
    location: Location
    source: str
    file_sha256: str

@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    kind: UnitKind
    language: SourceLanguage
    qualified_name: str
    member_symbol_ids: tuple[str, ...]
    fragments: tuple[SourceFragment, ...]
    imports: tuple[str, ...]
    reason: str

@dataclass(frozen=True)
class ExternalDependency:
    name: str
    version: str | None
    evidence: Location

@dataclass(frozen=True)
class ResourceReference:
    path: str | None
    expression: str
    exists: bool | None
    evidence: Location

@dataclass(frozen=True)
class Reference:
    spelling: str
    kind: str
    scope_id: str | None
    location: Location

@dataclass(frozen=True)
class ParsedFile:
    file_path: str
    sha256: str
    symbols: tuple[SourceSymbol, ...]
    imports: tuple[str, ...]
    references: tuple[Reference, ...]
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class SourceInventory:
    repo_root: Path
    language: SourceLanguage
    files: dict[str, ParsedFile]
    symbols: dict[str, SourceSymbol]
    graph_bindings: dict[str, str]
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class InternalDependency:
    target: SourceSymbol
    kind: DependencyKind
    evidence: Location
    origin: Origin

@dataclass(frozen=True)
class DependencyScan:
    internal_dependencies: tuple[InternalDependency, ...]
    external_dependencies: tuple[ExternalDependency, ...]
    resources: tuple[ResourceReference, ...]
    diagnostics: tuple[Diagnostic, ...]
