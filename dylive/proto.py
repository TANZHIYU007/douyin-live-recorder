"""最小化的 protobuf wire-format 编解码器（不依赖 protoc / protobuf 库）。

抖音弹幕走 protobuf，但字段编号会随着 webcast SDK 版本小幅变动。这里刻意不做
强类型 schema，而是把消息解成 ``{字段号: [原始值, ...]}``，再由 messages.py
按需取值。这样某个字段号变了只会让那一个字段取到默认值，不会让整条消息解析失败。
"""

from __future__ import annotations

import gzip
import struct
from typing import Any, Dict, List, Tuple

VARINT, I64, LEN, SGROUP, EGROUP, I32 = 0, 1, 2, 3, 4, 5

Fields = Dict[int, List[Any]]


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    n = len(buf)
    while True:
        if pos >= n:
            raise EOFError("varint 被截断")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def decode(buf: bytes) -> Fields:
    """解析一条 protobuf 消息，返回 {字段号: [值, ...]}。"""
    out: Fields = {}
    pos, n = 0, len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        tag, wire = key >> 3, key & 7
        if tag == 0:
            raise ValueError("非法字段号 0")
        if wire == VARINT:
            val, pos = _read_varint(buf, pos)
        elif wire == I64:
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wire == LEN:
            size, pos = _read_varint(buf, pos)
            if pos + size > n:
                raise EOFError("长度字段越界")
            val = buf[pos:pos + size]
            pos += size
        elif wire == I32:
            val = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:
            raise ValueError("不支持的 wire type %d" % wire)
        out.setdefault(tag, []).append(val)
    return out


def try_decode(buf: bytes) -> Fields:
    """解析失败时返回空字典，供不确定的嵌套字段使用。"""
    try:
        return decode(buf)
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 取值助手：字段缺失或 wire type 不符时一律回落到默认值
# --------------------------------------------------------------------------

def u(f: Fields, tag: int, default: int = 0) -> int:
    vals = f.get(tag)
    if not vals:
        return default
    val = vals[0]
    return val if isinstance(val, int) else default


def s(f: Fields, tag: int, default: str = "") -> str:
    vals = f.get(tag)
    if not vals:
        return default
    val = vals[0]
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if isinstance(val, int):
        return str(val)
    return default


def raw(f: Fields, tag: int) -> bytes:
    vals = f.get(tag)
    if vals and isinstance(vals[0], bytes):
        return vals[0]
    return b""


def sub(f: Fields, tag: int) -> Fields:
    data = raw(f, tag)
    return try_decode(data) if data else {}


def subs(f: Fields, tag: int) -> List[Fields]:
    return [try_decode(v) for v in f.get(tag, ()) if isinstance(v, bytes)]


# --------------------------------------------------------------------------
# 编码：只需要拼心跳包和 ack 包
# --------------------------------------------------------------------------

def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def enc_varint(tag: int, value: int) -> bytes:
    return _write_varint(tag << 3 | VARINT) + _write_varint(value)


def enc_bytes(tag: int, value: bytes) -> bytes:
    return _write_varint(tag << 3 | LEN) + _write_varint(len(value)) + value


def maybe_gunzip(data: bytes) -> bytes:
    """payload 可能是 gzip 压缩的，按魔数判断。"""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except Exception:
            return data
    return data
