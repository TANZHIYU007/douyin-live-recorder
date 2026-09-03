"""把抖音 WebSocket 二进制帧翻译成结构化弹幕事件。

外层是 PushFrame，payload 解压后是 Response，Response 里带一批 Message，
每个 Message 的 payload 才是具体业务消息（弹幕/礼物/进场……）。

字段号来自对线上流量的观察，不是官方文档。所有取值都走 proto.py 的容错助手：
某个字段号失效时只会让那一项为空，不会影响整条消息。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import proto

# ---- PushFrame ----
PF_LOG_ID, PF_PAYLOAD_ENCODING, PF_PAYLOAD_TYPE, PF_PAYLOAD = 2, 6, 7, 8

# ---- Response ----
RS_MESSAGES, RS_CURSOR, RS_INTERNAL_EXT, RS_NEED_ACK = 1, 2, 5, 9

# ---- Message ----
MSG_METHOD, MSG_PAYLOAD, MSG_ID = 1, 2, 3

# ---- Common ----
CM_METHOD, CM_MSG_ID, CM_ROOM_ID, CM_CREATE_TIME, CM_DESCRIBE = 1, 2, 3, 4, 7

# ---- User ----
US_ID, US_NICKNAME, US_GENDER, US_LEVEL = 1, 3, 4, 6
US_PAY_GRADE, US_DISPLAY_ID, US_FANS_CLUB, US_SEC_UID = 23, 38, 42, 46

HEARTBEAT = proto.enc_bytes(PF_PAYLOAD_TYPE, b"hb")

# method -> 事件类型；不在表里的消息一律忽略
KIND_BY_METHOD = {
    "WebcastChatMessage": "chat",
    "WebcastEmojiChatMessage": "emoji",
    "WebcastGiftMessage": "gift",
    "WebcastMemberMessage": "member",
    "WebcastLikeMessage": "like",
    "WebcastSocialMessage": "social",
    "WebcastRoomUserSeqMessage": "user_seq",
    "WebcastRoomStatsMessage": "stats",
    "WebcastControlMessage": "control",
    "WebcastFansclubMessage": "fansclub",
    "WebcastRoomMessage": "room_notice",
}

# 默认只落盘这几类，其余用 --kinds 打开
DEFAULT_KINDS = ("chat", "emoji", "gift", "social", "member", "control")


@dataclass
class Event:
    """一条录到的直播间事件。"""

    ts: float                       # 本地收到的时间（epoch 秒）
    offset: float                   # 相对本次录制开始的秒数
    kind: str                       # chat / gift / member / ...
    method: str                     # 抖音原始 method 名
    user_id: str = ""
    user_name: str = ""
    content: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        d["ts"] = round(self.ts, 3)
        d["offset"] = round(self.offset, 3)
        if not d["extra"]:
            d.pop("extra")
        return json.dumps(d, ensure_ascii=False)


def decode_frame(data: bytes) -> Tuple[List[Tuple[str, bytes]], Optional[bytes]]:
    """解析一个 PushFrame。

    返回 (消息列表, ack包)。ack 为 None 表示这帧不需要回执；浏览器引擎下
    页面自己会回 ack，调用方直接丢弃即可。
    """
    frame = proto.try_decode(data)
    payload = proto.raw(frame, PF_PAYLOAD)
    if not payload:
        return [], None

    body = proto.try_decode(proto.maybe_gunzip(payload))
    if not body:
        return [], None

    out: List[Tuple[str, bytes]] = []
    for m in proto.subs(body, RS_MESSAGES):
        method = proto.s(m, MSG_METHOD)
        blob = proto.raw(m, MSG_PAYLOAD)
        if method and blob:
            out.append((method, blob))

    ack = None
    if proto.u(body, RS_NEED_ACK):
        internal_ext = proto.s(body, RS_INTERNAL_EXT)
        ack = (proto.enc_varint(PF_LOG_ID, proto.u(frame, PF_LOG_ID))
               + proto.enc_bytes(PF_PAYLOAD_TYPE, b"ack")
               + proto.enc_bytes(PF_PAYLOAD, internal_ext.encode("utf-8")))
    return out, ack


def _user(f: proto.Fields, tag: int = 2) -> Tuple[str, str, Dict[str, Any]]:
    """从消息里取用户，返回 (uid, 昵称, 附加信息)。"""
    us = proto.sub(f, tag)
    if not us:
        return "", "", {}

    extra: Dict[str, Any] = {}
    display_id = proto.s(us, US_DISPLAY_ID)
    if display_id:
        extra["douyin_id"] = display_id
    sec_uid = proto.s(us, US_SEC_UID)
    if sec_uid:
        extra["sec_uid"] = sec_uid
    gender = proto.u(us, US_GENDER)
    if gender:
        extra["gender"] = gender

    pay_level = proto.u(proto.sub(us, US_PAY_GRADE), 6)
    if pay_level:
        extra["pay_level"] = pay_level

    club = proto.sub(proto.sub(us, US_FANS_CLUB), 1)
    club_name = proto.s(club, 1)
    if club_name:
        extra["fans_club"] = club_name
        extra["fans_level"] = proto.u(club, 2)

    return proto.s(us, US_ID), proto.s(us, US_NICKNAME), extra


def build_event(method: str, payload: bytes, ts: float, start: float) -> Optional[Event]:
    """把一条业务消息转成 Event；不关心的类型返回 None。"""
    kind = KIND_BY_METHOD.get(method)
    if kind is None:
        return None

    f = proto.try_decode(payload)
    if not f:
        return None

    common = proto.sub(f, 1)
    describe = proto.s(common, CM_DESCRIBE)
    ev = Event(ts=ts, offset=ts - start, kind=kind, method=method)

    msg_id = proto.s(common, CM_MSG_ID)
    if msg_id:
        ev.extra["msg_id"] = msg_id

    if kind == "chat":
        ev.user_id, ev.user_name, u_extra = _user(f)
        ev.content = proto.s(f, 3)
        ev.extra.update(u_extra)

    elif kind == "emoji":
        ev.user_id, ev.user_name, u_extra = _user(f)
        # defaultContent 是纯文本兜底，emojiContent 里是图片表情
        ev.content = proto.s(f, 5) or describe or "[表情]"
        ev.extra.update(u_extra)
        ev.extra["emoji_id"] = proto.s(f, 3)

    elif kind == "gift":
        ev.user_id, ev.user_name, u_extra = _user(f, 7)
        ev.extra.update(u_extra)
        gift = proto.sub(f, 15)
        gift_name = proto.s(gift, 16) or proto.s(gift, 2)
        count = proto.u(f, 5) or proto.u(f, 6) or 1      # repeatCount / comboCount
        ev.content = describe or ("送出 %s x%d" % (gift_name or "礼物", count))
        ev.extra.update({
            "gift_id": proto.s(f, 2),
            "gift_name": gift_name,
            "count": count,
            "group_count": proto.u(f, 4),
        })

    elif kind == "member":
        ev.user_id, ev.user_name, u_extra = _user(f)
        ev.extra.update(u_extra)
        ev.content = describe or ("%s 来了" % ev.user_name)
        ev.extra["member_count"] = proto.u(f, 3)
        ev.extra["action"] = proto.u(f, 10)

    elif kind == "like":
        ev.user_id, ev.user_name, u_extra = _user(f, 5)
        ev.extra.update(u_extra)
        count = proto.u(f, 2)
        ev.content = describe or ("点赞 x%d" % count)
        ev.extra.update({"count": count, "total": proto.u(f, 3)})

    elif kind == "social":
        ev.user_id, ev.user_name, u_extra = _user(f)
        ev.extra.update(u_extra)
        ev.content = describe or "关注了主播"
        ev.extra["follow_count"] = proto.u(f, 6)

    elif kind == "user_seq":
        ev.content = describe
        ev.extra.update({
            "online": proto.u(f, 3),
            "total_user": proto.u(f, 7),
            "total_user_str": proto.s(f, 8),
        })

    elif kind == "stats":
        ev.content = proto.s(f, 4) or proto.s(f, 3) or describe
        ev.extra["display_value"] = proto.u(f, 5)

    elif kind == "control":
        status = proto.u(f, 2)
        ev.content = describe or ("直播间状态 %d" % status)
        ev.extra["status"] = status

    elif kind == "fansclub":
        ev.user_id, ev.user_name, u_extra = _user(f, 3)
        ev.extra.update(u_extra)
        ev.content = proto.s(f, 2) or describe

    elif kind == "room_notice":
        ev.content = proto.s(proto.sub(f, 6), 2) or describe

    if not ev.content and not ev.user_name:
        return None
    return ev


def is_stream_ended(ev: Event) -> bool:
    """ControlMessage status 3/4 表示主播下播或被中断。"""
    return ev.kind == "control" and ev.extra.get("status") in (3, 4)
