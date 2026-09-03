"""原生引擎：直接连抖音的弹幕 WebSocket，不开浏览器。

内存和 CPU 都比浏览器引擎省一大截，代价是要自己算 signature —— 那个参数由抖音
的混淆 JS（webmssdk）现场生成，风控一更新就得跟着改。所以这条路默认不启用。

签名有两种来源：
  1. 项目根目录放一个 assets/sign.js，导出 sign(stub) 并把结果打到 stdout，
     本模块会用 node 调它（推荐，能跟着抖音更新自己换文件）；
  2. 都没有时退回裸 MD5，能不能连上看运气。
"""

from __future__ import annotations

import hashlib
import logging
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

from . import messages
from .messages import Event
from .paths import NO_WINDOW
from .utils import UA

log = logging.getLogger("danmaku.native")

WSS_HOST = "wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/"
SIGN_JS = Path(__file__).resolve().parent.parent / "assets" / "sign.js"

# 参与签名的字段，顺序不能改
SIGN_KEYS = ["live_id", "aid", "version_code", "webcast_sdk_version", "room_id",
             "sub_room_id", "sub_channel_id", "did_rule", "user_unique_id",
             "device_platform", "device_type", "ac", "identity"]

HEARTBEAT_INTERVAL = 10.0
MAX_HANDSHAKE_FAILURES = 3      # 签名不对的话再重试也没用，早点报错走人


def _user_unique_id() -> str:
    return str(random.randint(7300000000000000000, 7999999999999999999))


def _sign(params: dict) -> str:
    """先按抖音的规则拼出 X-MS-STUB，再交给 sign.js（若存在）。"""
    stub_src = ",".join("%s=%s" % (k, params.get(k, "")) for k in SIGN_KEYS)
    stub = hashlib.md5(stub_src.encode()).hexdigest()

    node = shutil.which("node")
    if node and SIGN_JS.exists():
        try:
            out = subprocess.run([node, str(SIGN_JS), stub],
                                 capture_output=True, timeout=15, check=True,
                                 **NO_WINDOW)
            sig = out.stdout.decode("utf-8", "replace").strip()
            if sig:
                log.debug("sign.js 生成签名 %s", sig)
                return sig
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("调用 sign.js 失败（%s），退回裸 MD5", exc)
    else:
        log.warning("未找到 node 或 assets/sign.js，签名退回裸 MD5，"
                    "连接可能被抖音拒绝；建议改用 --engine browser")
    return stub


class NativeEngine:
    """直连弹幕 WebSocket，断线自动重连。"""

    name = "native"

    def __init__(
        self,
        room_id: str,
        on_event: Callable[[Event], None],
        cookie: str = "",
        proxy: str = "",
        reconnect_delay: float = 5.0,
    ):
        if not room_id:
            raise ValueError("原生引擎需要真实 room_id（不是直播间短号 web_rid）")
        self.room_id = room_id
        self.on_event = on_event
        self.cookie = cookie
        self.proxy = proxy
        self.reconnect_delay = reconnect_delay

        self.start_time = time.time()
        self.frames = 0
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._error: Optional[BaseException] = None
        self._last_error = ""
        self._failures = 0

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dm-native", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:                   # noqa: BLE001 - 收尾失败无所谓
                pass
        if self._thread:
            self._thread.join(timeout=15)
            self._thread = None

    def wait_connected(self, timeout: float = 30.0) -> bool:
        return self.connected.wait(timeout)

    @property
    def failed(self) -> Optional[BaseException]:
        return self._error

    # -- 连接 -------------------------------------------------------------

    def build_url(self) -> str:
        params = {
            "app_name": "douyin_web",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "update_version_code": "1.0.14-beta.0",
            "compress": "gzip",
            "device_platform": "web",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Mozilla",
            "browser_version": UA.split("Mozilla/", 1)[-1],
            "browser_online": "true",
            "tz_name": "Asia/Shanghai",
            "cursor": "",
            "internal_ext": "",
            "host": "https://live.douyin.com",
            "aid": "6383",
            "live_id": "1",
            "did_rule": "3",
            "endpoint": "live_pc",
            "support_wrds": "1",
            "user_unique_id": _user_unique_id(),
            "im_path": "/webcast/im/fetch/",
            "identity": "audience",
            "need_persist_msg_count": "15",
            "insert_task_id": "",
            "live_reason": "",
            "room_id": self.room_id,
            "heartbeatDuration": "0",
        }
        params["signature"] = _sign(params)
        return WSS_HOST + "?" + urlencode(params)

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            self._error = RuntimeError(
                "缺少 websocket-client。请执行 pip install websocket-client，"
                "或改用 --engine browser。")
            log.error("%s", self._error)
            return

        while not self._stop.is_set():
            ever_connected = False
            try:
                self._connect_once(websocket)
                ever_connected = self.connected.is_set()
            except Exception as exc:            # noqa: BLE001 - 重连循环兜住一切
                if self._stop.is_set():
                    break
                log.warning("弹幕连接异常：%s", exc)
            if self._stop.is_set():
                break

            self.connected.clear()
            self._failures = 0 if ever_connected else self._failures + 1
            if self._failures >= MAX_HANDSHAKE_FAILURES:
                self._error = RuntimeError(self._handshake_hint())
                log.error("%s", self._error)
                return
            log.info("%.0f 秒后重连弹幕", self.reconnect_delay)
            self._stop.wait(self.reconnect_delay)

    def _handshake_hint(self) -> str:
        if "DEVICE_BLOCKED" in self._last_error or "415" in self._last_error:
            return ("抖音拒绝了弹幕连接（DEVICE_BLOCKED），说明 signature 没算对。"
                    "放一个可用的 assets/sign.js，或者直接改用 --engine browser。")
        return ("连续 %d 次连不上弹幕服务器，最后一次错误：%s"
                % (MAX_HANDSHAKE_FAILURES, self._last_error or "未知"))

    def _connect_once(self, websocket_mod) -> None:
        url = self.build_url()
        headers = ["User-Agent: " + UA]
        if self.cookie:
            headers.append("Cookie: " + self.cookie)

        ws = websocket_mod.WebSocketApp(
            url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=lambda _ws, code, msg: log.info("弹幕连接关闭 %s %s", code, msg),
        )
        self._ws = ws

        kwargs = {"origin": "https://live.douyin.com", "ping_interval": 0}
        if self.proxy:
            kwargs.update(self._proxy_kwargs())
        ws.run_forever(**kwargs)

    def _proxy_kwargs(self) -> dict:
        # 形如 http://127.0.0.1:7890
        rest = self.proxy.split("://", 1)[-1]
        host, _, port = rest.partition(":")
        return {"http_proxy_host": host, "http_proxy_port": int(port or 80),
                "proxy_type": self.proxy.split("://", 1)[0] or "http"}

    # -- 回调 -------------------------------------------------------------

    def _on_error(self, _ws, err) -> None:
        # 握手被拒时抖音会在响应头里写明原因，留着给 _handshake_hint 判断
        self._last_error = str(err)
        log.debug("ws error: %s", self._last_error[:200])

    def _on_open(self, ws) -> None:
        log.info("弹幕 WebSocket 已连接（room_id=%s）", self.room_id)
        self.connected.set()
        threading.Thread(target=self._heartbeat, args=(ws,),
                         name="dm-heartbeat", daemon=True).start()

    def _heartbeat(self, ws) -> None:
        import websocket
        while not self._stop.is_set() and ws.sock and ws.sock.connected:
            try:
                ws.send(messages.HEARTBEAT, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as exc:            # noqa: BLE001
                log.debug("发送心跳失败：%s", exc)
                return
            self._stop.wait(HEARTBEAT_INTERVAL)

    def _on_message(self, ws, data) -> None:
        if isinstance(data, str):
            return
        self.frames += 1
        try:
            msgs, ack = messages.decode_frame(data)
        except Exception as exc:                # noqa: BLE001
            log.debug("解析弹幕帧失败：%s", exc)
            return

        if ack:
            try:
                import websocket
                ws.send(ack, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as exc:            # noqa: BLE001
                log.debug("回 ack 失败：%s", exc)

        now = time.time()
        for method, payload in msgs:
            try:
                ev = messages.build_event(method, payload, now, self.start_time)
            except Exception as exc:            # noqa: BLE001
                log.debug("解析 %s 失败：%s", method, exc)
                continue
            if ev is not None:
                self.on_event(ev)
