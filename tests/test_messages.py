"""PushFrame -> Response -> 业务消息 -> Event 这条解码链。

「画面正常、弹幕一条没有」的故障里，除了压根没连上，剩下的就断在这里。
所以这些用例要钉死两件事：能认的消息必须认出来，认不出来的必须安静地跳过，
而不是把整帧带崩。
"""

import gzip

import pytest

from dylive import messages, proto


# --------------------------------------------------------------------------
# 造帧的小工具
# --------------------------------------------------------------------------

def f(tag, val) -> bytes:
    if isinstance(val, bool):
        raise TypeError("别传 bool，会被当成 int")
    if isinstance(val, int):
        return proto.enc_varint(tag, val)
    if isinstance(val, str):
        return proto.enc_bytes(tag, val.encode("utf-8"))
    return proto.enc_bytes(tag, val)


def common(msg_id: str = "7300000000000000001", describe: str = "") -> bytes:
    out = f(2, msg_id)
    if describe:
        out += f(7, describe)
    return out


def user(uid: str = "88", nickname: str = "路过的风", douyin_id: str = "dy12580",
         pay_level: int = 0, club: str = "", club_level: int = 0) -> bytes:
    out = f(1, uid) + f(3, nickname) + f(38, douyin_id)
    if pay_level:
        out += f(23, f(6, pay_level))
    if club:
        out += f(42, f(1, f(1, club) + f(2, club_level)))
    return out


def push_frame(pairs, need_ack: bool = False, internal_ext: str = "",
               log_id: int = 12345, gzipped: bool = False) -> bytes:
    body = b"".join(f(1, f(1, method) + f(2, payload)) for method, payload in pairs)
    if need_ack:
        body += f(5, internal_ext) + f(9, 1)
    if gzipped:
        body = gzip.compress(body)
    return f(2, log_id) + f(8, body)


# --------------------------------------------------------------------------
# 单条业务消息
# --------------------------------------------------------------------------

def test_聊天消息():
    payload = f(1, common()) + f(2, user(pay_level=24)) + f(3, "这个录制工具好用啊")
    ev = messages.build_event("WebcastChatMessage", payload, ts=1000.0, start=940.0)

    assert ev is not None
    assert ev.kind == "chat"
    assert ev.user_name == "路过的风"
    assert ev.user_id == "88"
    assert ev.content == "这个录制工具好用啊"
    assert ev.offset == pytest.approx(60.0)
    assert ev.extra["douyin_id"] == "dy12580"
    assert ev.extra["pay_level"] == 24
    assert ev.extra["msg_id"] == "7300000000000000001"


def test_聊天消息带粉丝团():
    payload = f(1, common()) + f(2, user(club="夜航团", club_level=7)) + f(3, "晚上好")
    ev = messages.build_event("WebcastChatMessage", payload, 1.0, 0.0)
    assert ev.extra["fans_club"] == "夜航团"
    assert ev.extra["fans_level"] == 7


def test_礼物消息():
    payload = (f(1, common()) + f(2, "gift-1") + f(5, 10)
               + f(7, user(nickname="橘子汽水")) + f(15, f(16, "小心心")))
    ev = messages.build_event("WebcastGiftMessage", payload, 1.0, 0.0)

    assert ev.kind == "gift"
    assert ev.user_name == "橘子汽水"
    assert ev.content == "送出 小心心 x10"
    assert ev.extra["gift_name"] == "小心心"
    assert ev.extra["count"] == 10


def test_礼物消息优先用抖音给的描述():
    payload = (f(1, common(describe="橘子汽水送出了小心心")) + f(5, 10)
               + f(7, user()) + f(15, f(16, "小心心")))
    ev = messages.build_event("WebcastGiftMessage", payload, 1.0, 0.0)
    assert ev.content == "橘子汽水送出了小心心"


def test_进场消息():
    payload = f(1, common()) + f(2, user(nickname="晚八点")) + f(3, 4200)
    ev = messages.build_event("WebcastMemberMessage", payload, 1.0, 0.0)
    assert ev.kind == "member"
    assert ev.content == "晚八点 来了"
    assert ev.extra["member_count"] == 4200


def test_关注消息():
    payload = f(1, common()) + f(2, user()) + f(6, 512)
    ev = messages.build_event("WebcastSocialMessage", payload, 1.0, 0.0)
    assert ev.kind == "social"
    assert ev.content == "关注了主播"
    assert ev.extra["follow_count"] == 512


def test_在线人数消息没有用户也算有效():
    payload = f(1, common()) + f(3, 1234) + f(8, "1.2万")
    ev = messages.build_event("WebcastRoomUserSeqMessage", payload, 1.0, 0.0)
    assert ev.kind == "user_seq"
    assert ev.extra["online"] == 1234
    assert ev.extra["total_user_str"] == "1.2万"


@pytest.mark.parametrize("status,ended", [(1, False), (2, False), (3, True), (4, True)])
def test_下播判定(status, ended):
    payload = f(1, common()) + f(2, status)
    ev = messages.build_event("WebcastControlMessage", payload, 1.0, 0.0)
    assert ev.extra["status"] == status
    assert messages.is_stream_ended(ev) is ended


def test_不关心的消息类型返回_None():
    payload = f(1, common()) + f(2, user())
    assert messages.build_event("WebcastSomeFutureMessage", payload, 1.0, 0.0) is None


def test_空壳消息返回_None():
    """没有内容也没有用户名的消息不值得落盘。"""
    assert messages.build_event("WebcastChatMessage", f(1, common()), 1.0, 0.0) is None


def test_字段编号变了只让那一个字段变空():
    """抖音把昵称从 3 挪到 31 的话，content 不该跟着丢。

    这正是 proto.py 刻意不做强类型 schema 的理由。
    """
    payload = f(1, common()) + f(2, f(1, "88") + f(31, "新编号昵称")) + f(3, "内容还在")
    ev = messages.build_event("WebcastChatMessage", payload, 1.0, 0.0)
    assert ev.content == "内容还在"
    assert ev.user_name == ""


def test_载荷是垃圾时返回_None而不是抛异常():
    assert messages.build_event("WebcastChatMessage", b"\xff\xff\xff", 1.0, 0.0) is None


# --------------------------------------------------------------------------
# 整帧
# --------------------------------------------------------------------------

def test_解析一帧里的多条消息():
    chat = f(1, common()) + f(2, user()) + f(3, "第一条")
    gift = f(1, common()) + f(5, 1) + f(7, user()) + f(15, f(16, "玫瑰"))
    msgs, ack = messages.decode_frame(
        push_frame([("WebcastChatMessage", chat), ("WebcastGiftMessage", gift)]))

    assert [m[0] for m in msgs] == ["WebcastChatMessage", "WebcastGiftMessage"]
    assert ack is None              # 没要求回执


def test_gzip_载荷():
    chat = f(1, common()) + f(2, user()) + f(3, "压缩过的")
    msgs, _ = messages.decode_frame(
        push_frame([("WebcastChatMessage", chat)], gzipped=True))
    ev = messages.build_event(msgs[0][0], msgs[0][1], 1.0, 0.0)
    assert ev.content == "压缩过的"


def test_需要回执时给出_ack_包():
    frame = push_frame([], need_ack=True, internal_ext="cursor-abc", log_id=999)
    _, ack = messages.decode_frame(frame)

    assert ack is not None
    fields = proto.decode(ack)
    assert proto.u(fields, 2) == 999                    # log_id 原样带回
    assert proto.raw(fields, 7) == b"ack"
    assert proto.s(fields, 8) == "cursor-abc"


@pytest.mark.parametrize("bad", [b"", b"\xff\xff", b"\x08\x01", proto.enc_bytes(8, b"\xff\xff")])
def test_坏帧安静地返回空(bad):
    msgs, ack = messages.decode_frame(bad)
    assert msgs == []
    assert ack is None


def test_心跳包是合法_protobuf():
    fields = proto.decode(messages.HEARTBEAT)
    assert proto.raw(fields, 7) == b"hb"
