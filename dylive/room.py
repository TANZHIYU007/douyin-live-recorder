"""获取抖音直播间信息与拉流地址。

优先走 web enter 接口（返回干净 JSON），失败再退回解析直播间页面 HTML。
两条路都可能因抖音风控调整而失效，所以浏览器引擎下会直接复用页面自己发出的
enter 请求响应（见 danmaku_browser.py），那条路最稳。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from .utils import UA

log = logging.getLogger("room")

ENTER_API = "https://live.douyin.com/webcast/room/web/enter/"

# 画质从高到低；origin 是原画，只在 live_core_sdk_data 里给
QUALITY_ORDER = ["origin", "FULL_HD1", "HD1", "SD1", "SD2"]
QUALITY_ALIAS = {
    "origin": "origin", "原画": "origin",
    "blueray": "FULL_HD1", "蓝光": "FULL_HD1", "full_hd1": "FULL_HD1",
    "uhd": "HD1", "超清": "HD1", "hd1": "HD1",
    "hd": "SD1", "高清": "SD1", "sd1": "SD1",
    "sd": "SD2", "标清": "SD2", "sd2": "SD2",
}

STATUS_LIVING = 2


@dataclass
class RoomInfo:
    web_rid: str
    room_id: str = ""
    title: str = ""
    nickname: str = ""
    status: int = 0
    flv: Dict[str, str] = field(default_factory=dict)
    hls: Dict[str, str] = field(default_factory=dict)
    cover: str = ""                 # 直播间封面图，给界面用
    avatar: str = ""                # 主播头像
    online: str = ""                # 在线人数（抖音给的是格式化好的字符串）
    ok: bool = False                # 这次查询本身成没成功

    # 断网时 status 也是 0，跟「确认未开播」长得一模一样。录制中必须能区分这两者：
    # 前者要继续重试，后者才该收工。所以查询成功与否单独用 ok 表达。

    @property
    def living(self) -> bool:
        return self.status == STATUS_LIVING and bool(self.flv or self.hls)

    def pick(self, quality: str = "origin", prefer: str = "flv") -> Optional[str]:
        """按画质挑一条流地址，指定画质拿不到就顺次降级。"""
        want = QUALITY_ALIAS.get(quality.lower(), "origin")
        order = QUALITY_ORDER[QUALITY_ORDER.index(want):] + QUALITY_ORDER
        maps = (self.flv, self.hls) if prefer == "flv" else (self.hls, self.flv)
        for m in maps:
            for q in order:
                if m.get(q):
                    return m[q]
        return None

    def describe(self) -> str:
        if not self.ok:
            return "[%s] 查询失败（网络或风控）" % self.web_rid
        state = "直播中" if self.living else "未开播(status=%s)" % self.status
        return "[%s] %s | %s | %s" % (self.web_rid, self.nickname or "-",
                                      self.title or "-", state)


def parse_target(target: str, session: Optional[requests.Session] = None) -> str:
    """把用户输入（房间号 / 直播间链接 / v.douyin.com 短链）统一成 web_rid。"""
    target = target.strip()
    if target.isdigit():
        return target

    if "v.douyin.com" in target:
        sess = session or make_session()
        target = sess.get(target, allow_redirects=True, timeout=10).url
        log.info("短链跳转到 %s", target)

    m = re.search(r"live\.douyin\.com/(\d+)", target)
    if m:
        return m.group(1)
    m = re.search(r"[?&]web_rid=(\d+)", target)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", urlparse(target).path)
    if m:
        return m.group(1)
    raise ValueError("无法从 %r 中解析出直播间号" % target)


def make_session(cookie: str = "") -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Referer": "https://live.douyin.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if cookie:
        sess.headers["Cookie"] = cookie
    else:
        try:
            sess.get("https://live.douyin.com/", timeout=10)   # 首页下发 ttwid
        except requests.RequestException as exc:
            log.debug("预热 cookie 失败：%s", exc)
    return sess


def _stream_maps(stream_url: Dict[str, Any]):
    """从 stream_url 结构里抽出 {画质: 地址} 两张表。"""
    flv = dict(stream_url.get("flv_pull_url") or {})
    hls = dict(stream_url.get("hls_pull_url_map") or {})

    # 原画只出现在这个内嵌 JSON 字符串里
    try:
        blob = stream_url["live_core_sdk_data"]["pull_data"]["stream_data"]
        for key, val in json.loads(blob).get("data", {}).items():
            main = val.get("main") or {}
            if main.get("flv"):
                flv.setdefault(key, main["flv"])
            if main.get("hls"):
                hls.setdefault(key, main["hls"])
    except (KeyError, TypeError, ValueError) as exc:
        log.debug("解析 live_core_sdk_data 失败：%s", exc)

    return ({k: v for k, v in flv.items() if v},
            {k: v for k, v in hls.items() if v})


def from_enter_payload(payload: Dict[str, Any], web_rid: str = "") -> Optional[RoomInfo]:
    """把 enter 接口的 JSON 转成 RoomInfo。浏览器引擎也复用这个函数。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rooms = data.get("data")
    if not rooms:
        return None
    room = rooms[0] if isinstance(rooms, list) else rooms

    info = RoomInfo(web_rid=web_rid or str(data.get("web_rid") or ""))
    info.room_id = str(room.get("id_str") or room.get("id") or "")
    info.title = room.get("title") or ""
    info.status = int(room.get("status") or 0)
    user = data.get("user") or room.get("owner") or {}
    info.nickname = user.get("nickname") or ""
    info.online = room.get("user_count_str") or ""
    info.cover = _first_url(room.get("cover"))
    info.avatar = _first_url(user.get("avatar_thumb") or user.get("avatar_medium"))
    info.flv, info.hls = _stream_maps(room.get("stream_url") or {})
    info.ok = True
    return info


def _first_url(node: Any) -> str:
    if isinstance(node, dict):
        urls = node.get("url_list")
        if isinstance(urls, list) and urls:
            return str(urls[0])
    return ""


def _fetch_via_api(sess: requests.Session, web_rid: str) -> Optional[RoomInfo]:
    params = {
        "aid": "6383", "app_name": "douyin_web", "live_id": "1",
        "device_platform": "web", "language": "zh-CN", "enter_from": "web_live",
        "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
        "browser_language": "zh-CN", "browser_platform": "Win32",
        "browser_name": "Chrome", "browser_version": "131.0.0.0",
        "web_rid": web_rid, "room_id_str": "", "enter_source": "",
        "is_need_double_stream": "false", "insert_task_id": "", "live_reason": "",
    }
    resp = sess.get(ENTER_API, params=params,
                    headers={"Referer": "https://live.douyin.com/%s" % web_rid},
                    timeout=15)
    resp.raise_for_status()
    return from_enter_payload(resp.json(), web_rid)


_UNESCAPE = [("\\u002F", "/"), ("\\u002f", "/"), ("\\u0026", "&"),
             ("\\\\/", "/"), ("\\/", "/"), ('\\\\"', '"'), ('\\"', '"')]


def _fetch_via_html(sess: requests.Session, web_rid: str) -> Optional[RoomInfo]:
    """页面里嵌着房间 JSON，但转义层数随版本变，所以对反转义后的文本做正则。"""
    resp = sess.get("https://live.douyin.com/%s" % web_rid, timeout=15)
    resp.raise_for_status()
    html = resp.text
    for old, new in _UNESCAPE:
        html = html.replace(old, new)

    info = RoomInfo(web_rid=web_rid)
    m = re.search(r'"status"\s*:\s*(\d+)', html)
    if m:
        info.status = int(m.group(1))
    m = re.search(r'"nickname"\s*:\s*"([^"]{1,60})"', html)
    if m:
        info.nickname = m.group(1)
    m = re.search(r'"title"\s*:\s*"([^"]{1,120})"', html)
    if m:
        info.title = m.group(1)
    m = re.search(r'"id_str"\s*:\s*"(\d{10,})"', html)
    if m:
        info.room_id = m.group(1)

    for key, target in (("flv_pull_url", info.flv), ("hls_pull_url_map", info.hls)):
        block = re.search(key + r'"\s*:\s*\{(.*?)\}', html, re.S)
        if not block:
            continue
        for q, url in re.findall(r'"(\w+)"\s*:\s*"(https?://[^"]+)"', block.group(1)):
            target[q] = url

    if not info.flv and not info.hls:
        for url in re.findall(r'"(https?://[^"\s]+?\.flv[^"\s]*)"', html):
            info.flv.setdefault("origin", url)
            break
    if info.flv or info.hls:
        info.status = info.status or STATUS_LIVING
    if not (info.flv or info.hls or info.status):
        return None
    info.ok = True
    return info


def fetch(web_rid: str, session: Optional[requests.Session] = None) -> RoomInfo:
    """查直播间当前状态，接口失败自动退回 HTML 解析。"""
    sess = session or make_session()
    try:
        info = _fetch_via_api(sess, web_rid)
        if info and (info.living or info.status):
            return info
        log.debug("enter 接口未给出有效数据，改用 HTML 解析")
    except Exception as exc:
        log.debug("enter 接口失败（%s），改用 HTML 解析", exc)

    try:
        info = _fetch_via_html(sess, web_rid)
        if info:
            return info
    except Exception as exc:
        log.warning("解析直播间页面失败：%s", exc)

    # 两条路都没走通：这是「查不到」，不是「没开播」
    return RoomInfo(web_rid=web_rid, ok=False)
