"""protobuf 编解码器。

这一层的要求只有一条：**喂它什么都不能抛到调用方**。抖音随时会改字段编号，
解码器必须把「这个字段读不出来」降级成「这个字段是空的」，而不是整条消息失败。
"""

import gzip

import pytest

from dylive import proto


def test_解码基本类型():
    buf = (proto.enc_varint(1, 150)
           + proto.enc_bytes(2, b"hello")
           + proto.enc_varint(3, 0))
    f = proto.decode(buf)
    assert proto.u(f, 1) == 150
    assert proto.s(f, 2) == "hello"
    assert proto.u(f, 3) == 0


def test_同一字段重复出现时全部保留():
    buf = proto.enc_bytes(1, b"a") + proto.enc_bytes(1, b"b")
    f = proto.decode(buf)
    assert len(f[1]) == 2
    assert proto.s(f, 1) == "a"          # 取值助手只看第一个


def test_嵌套消息():
    inner = proto.enc_bytes(3, "昵称".encode())
    f = proto.decode(proto.enc_bytes(2, inner))
    assert proto.s(proto.sub(f, 2), 3) == "昵称"


def test_subs_取重复的嵌套消息():
    one = proto.enc_bytes(1, b"x")
    two = proto.enc_bytes(1, b"y")
    f = proto.decode(proto.enc_bytes(5, one) + proto.enc_bytes(5, two))
    assert [proto.s(m, 1) for m in proto.subs(f, 5)] == ["x", "y"]


@pytest.mark.parametrize("bad", [
    b"\x08",                    # varint 被截断
    b"\x12\x05ab",              # 长度字段越界
    b"\x00\x01",                # 字段号 0
    b"\x1d\x01",                # I32 数据不够
    b"\x23",                    # 不支持的 wire type（3 = SGROUP）
])
def test_畸形输入让_decode_抛出(bad):
    with pytest.raises((EOFError, ValueError, IndexError)):
        proto.decode(bad)


@pytest.mark.parametrize("bad", [
    b"\x08", b"\x12\x05ab", b"\x00\x01", b"\x1d\x01", b"\x23", b"\xff" * 20,
])
def test_try_decode_永远不抛(bad):
    assert proto.try_decode(bad) == {}


def test_取值助手在类型不符时回落到默认值():
    f = proto.decode(proto.enc_bytes(1, b"\xff\xfe") + proto.enc_varint(2, 7))
    assert proto.s(f, 1) == ""           # 不是合法 utf-8
    assert proto.s(f, 2) == "7"          # 数字转成字符串
    assert proto.u(f, 1) == 0            # bytes 当不成整数
    assert proto.raw(f, 2) == b""        # 整数当不成 bytes
    assert proto.sub(f, 99) == {}        # 字段压根不存在
    assert proto.subs(f, 99) == []


def test_maybe_gunzip():
    raw = b"abc" * 100
    assert proto.maybe_gunzip(gzip.compress(raw)) == raw
    assert proto.maybe_gunzip(raw) == raw                    # 没压缩就原样返回
    broken = b"\x1f\x8b" + "坏掉的".encode()     # 有 gzip 魔数但解不开
    assert proto.maybe_gunzip(broken) == broken


def test_varint_往返():
    for value in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 63 - 1):
        f = proto.decode(proto.enc_varint(9, value))
        assert proto.u(f, 9) == value
