"""共享源码原文、精确位置及内容一致性检查。"""
from __future__ import annotations
import hashlib
import io
import tokenize
from pathlib import Path
from .models import Diagnostic, Location, SourceFragment
from .codegraph import GraphError
TYPE_KINDS = {'class', 'struct', 'interface', 'enum'}

CALL_KINDS = {'function', 'method', 'constructor'}

def diagnostic(code, message, location=None, candidates=(), blocking=True):
    return Diagnostic(code, message, location, blocking, candidates=tuple(candidates))

def covers(outer: Location, inner: Location) -> bool:
    return outer.file_path == inner.file_path and outer.start_byte <= inner.start_byte <= inner.end_byte <= outer.end_byte

class SourceText:

    def __init__(self, file_path: str, raw: bytes, python=False):
        self.file_path, self.raw = (file_path, raw)
        self.encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if python else 'utf-8'
        self.text = raw.decode(self.encoding)
        self.codec = 'utf-8' if self.encoding == 'utf-8-sig' else self.encoding
        self.sha256 = hashlib.sha256(raw).hexdigest()

    def location(self, start: int, end: int) -> Location:
        if not 0 <= start <= end <= len(self.raw):
            raise ValueError('源码字节范围越界')
        first = self.raw[:start].count(b'\n') + 1
        last = self.raw[:max(start, end - 1)].count(b'\n') + 1
        return Location(self.file_path, first, last, start, end)

    def fragment(self, location: Location) -> SourceFragment:
        source = self.raw[location.start_byte:location.end_byte].decode(self.codec)
        return SourceFragment(location, source, self.sha256)



def key_id(key):
    return (key.get('name'), key.get('filePath'), key.get('startLine'))


def location(file, value, raw):
    if not isinstance(value, dict) or not {'startByte', 'endByte'} <= value.keys():
        raise GraphError('GRAPH_PROTOCOL_ERROR', '上游未提供精确的原文字节区间')
    start, end = value['startByte'], value['endByte']
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(raw):
        raise GraphError('GRAPH_PROTOCOL_ERROR', '上游返回的字节区间越界')
    return Location(file, raw[:start].count(b'\n') + 1, raw[:max(start, end - 1)].count(b'\n') + 1, start, end)


def checked_content(row, files, prefix=''):
    name = (Path(prefix) / row['filePath']).as_posix() if prefix else row['filePath']
    if name not in files:
        raise GraphError('GRAPH_PROTOCOL_ERROR', '上游读取了未授权的上下文文件：' + name)
    loc = location(name, row.get('range'), files[name])
    codec = SourceText(name, files[name], name.endswith(('.py', '.pyi'))).codec
    if files[name][loc.start_byte:loc.end_byte].decode(codec) != row['source']:
        raise ValueError('SOURCE_CHANGED：上游原文与本次快照不一致：' + name)
    return loc
