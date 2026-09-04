"""一个 Chromium 服务所有房间。

为什么不是每个房间一个浏览器：
  * 每个 Chromium 实例约 200-300 MB 内存，录五个房间就是 1.5 GB；
  * Playwright 的 sync API 不是线程安全的 —— 一个 playwright 对象上的所有调用
    都必须发生在创建它的那个线程上，多线程各开各的很容易踩坑。

所以这里用一个专属线程跑唯一的 playwright，房间的增删通过命令队列投递进去。
那个线程的主循环靠 ``page.wait_for_timeout()`` 驱动事件派发 —— 这点很关键，
用 threading 的 wait 会把 greenlet 调度器饿死，websocket 回调再也收不到
（这个坑踩过一次，见 README）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import messages, room, runtime
from .messages import Event
from .utils import UA

log = logging.getLogger("browser")

IM_PATH = "/webcast/im/push/"
ENTER_PATH = "/webcast/room/web/enter"

# 这些资源对抓弹幕毫无用处，直接掐掉省 CPU 和流量。
# 注意别误伤脚本和样式 —— 抖音的 JS 也挂在 CDN 的 /obj/ 路径下。
BLOCKED_TYPES = {"image", "media", "font"}
STREAM_HINTS = (".flv", ".m3u8")

WS_CONNECT_TIMEOUT = 45.0

# 被风控挡住时，抖音返回的是一个「验证码中间页」，直播间的 JS 压根不加载，
# 于是一个 WebSocket 都不会建 —— 表现就是画面正常录、弹幕一条都没有，而且
# goto 是成功返回的，不检查的话整场都不会有任何提示。
VERIFY_HINTS = ("验证码", "verify", "captcha")

# 页面打开失败（网络抖动、房间刚下播）时的重试策略。不重试的话该房间整场
# 都不会有弹幕，而画面还在录，很容易到事后才发现。
PAGE_RETRY_LIMIT = 3
PAGE_RETRY_DELAY = 8.0


class RoomChannel:
    """一个房间在 hub 里的句柄。接口刻意和早期的单房间引擎保持一致。"""

    def __init__(self, web_rid: str,
                 on_event: Callable[[Event], None],
                 on_room_info: Optional[Callable[[room.RoomInfo], None]] = None):
        self.web_rid = web_rid
        self.on_event = on_event
        self.on_room_info = on_room_info
        self.start_time = time.time()
        self.attached_at = time.time()  # 用来判断「等了多久还没连上」
        self.frames = 0
        self.connected = threading.Event()
        self.blocked = ""               # 非空表示被风控挡在验证页
        self.warned = False             # 「一直没连上」只喊一次
        self.page = None
        self._hub: Optional["BrowserHub"] = None

    def stop(self) -> None:
        if self._hub is not None:
            self._hub.detach(self.web_rid)

    def wait_connected(self, timeout: float = WS_CONNECT_TIMEOUT) -> bool:
        return self.connected.wait(timeout)

    @property
    def failed(self) -> Optional[BaseException]:
        return self._hub.failed if self._hub else None


class BrowserHub:
    def __init__(self, headless: bool = True, block_media: bool = True,
                 user_data_dir: str = "", cookie: str = "", proxy: str = ""):
        self.headless = headless
        self.block_media = block_media
        self.user_data_dir = user_data_dir
        self.cookie = cookie
        self.proxy = proxy

        self._rooms: Dict[str, RoomChannel] = {}
        self._rooms_lock = threading.Lock()
        self._commands: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._frames: "queue.Queue[Tuple[RoomChannel, bytes]]" = queue.Queue(maxsize=8192)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._retries: Dict[str, int] = {}
        self._pending: Dict[str, float] = {}    # rid -> 到点重开的时间
        self._threads: List[threading.Thread] = []
        self._error: Optional[BaseException] = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for target, name in ((self._run_browser, "hub"), (self._run_decoder, "decode")):
            t = threading.Thread(target=target, name="dm-" + name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=25)
        self._threads.clear()
        with self._rooms_lock:
            self._rooms.clear()

    def wait_ready(self, timeout: float = 60.0) -> bool:
        return self._ready.wait(timeout)

    @property
    def failed(self) -> Optional[BaseException]:
        return self._error

    @property
    def room_count(self) -> int:
        with self._rooms_lock:
            return len(self._rooms)

    # -- 房间增删 ---------------------------------------------------------

    def attach(self, web_rid: str, on_event, on_room_info=None) -> RoomChannel:
        channel = RoomChannel(web_rid, on_event, on_room_info)
        channel._hub = self
        with self._rooms_lock:
            old = self._rooms.get(web_rid)
            self._rooms[web_rid] = channel
        if old is not None:
            self._commands.put(("close_page", web_rid))
        self.start()
        self._commands.put(("open", web_rid))
        return channel

    def detach(self, web_rid: str) -> None:
        with self._rooms_lock:
            self._rooms.pop(web_rid, None)
        self._commands.put(("close_page", web_rid))

    def _channel(self, web_rid: str) -> Optional[RoomChannel]:
        with self._rooms_lock:
            return self._rooms.get(web_rid)

    # -- 解码线程 ---------------------------------------------------------

    def _run_decoder(self) -> None:
        while True:
            try:
                channel, data = self._frames.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return          # 队列排空了才退出，免得丢掉最后几帧弹幕
                continue
            try:
                msgs, _ = messages.decode_frame(data)   # ack 由页面自己发
            except Exception as exc:                    # noqa: BLE001
                log.debug("解析弹幕帧失败：%s", exc)
                continue
            now = time.time()
            for method, payload in msgs:
                try:
                    ev = messages.build_event(method, payload, now, channel.start_time)
                except Exception as exc:                # noqa: BLE001
                    log.debug("解析 %s 失败：%s", method, exc)
                    continue
                if ev is not None:
                    channel.on_event(ev)

    # -- 浏览器线程 -------------------------------------------------------

    def _run_browser(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._error = RuntimeError(
                "缺少 playwright。请执行：\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium")
            log.error("%s", self._error)
            self._ready.set()
            return

        runtime.apply_env()         # 指向内置浏览器目录（源码运行时是空操作）
        try:
            with sync_playwright() as pw:
                self._drive(pw)
        except Exception as exc:    # noqa: BLE001 - 线程边界统一兜住
            if not self._stop.is_set():
                self._error = exc
                log.error("浏览器异常退出：%s", exc)
        finally:
            self._ready.set()

    def _launch(self, pw):
        args = {
            # 钉住完整版 Chromium：新版 Playwright 的 headless 默认会去找单独的
            # headless shell，而打包时只带了完整版这一份
            "channel": "chromium",
            "headless": self.headless,
            "args": ["--mute-audio", "--disable-blink-features=AutomationControlled",
                     "--autoplay-policy=no-user-gesture-required"],
        }
        if self.proxy:
            args["proxy"] = {"server": self.proxy}
        context_opts = {
            "user_agent": UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 720},
        }
        if self.user_data_dir:
            # 持久化目录能保留登录态，而且所有房间共用一份登录
            return pw.chromium.launch_persistent_context(
                self.user_data_dir, **args, **context_opts), None
        browser = pw.chromium.launch(**args)
        return browser.new_context(**context_opts), browser

    def _drive(self, pw) -> None:
        context, browser = self._launch(pw)
        pages: Dict[str, object] = {}
        try:
            if self.cookie:
                context.add_cookies(_parse_cookies(self.cookie))
            if self.block_media:
                context.route("**/*", _block_heavy)

            # 一个空白页专门用来驱动事件派发，房间页开关都不影响它
            keeper = context.pages[0] if context.pages else context.new_page()
            self._ready.set()
            log.info("浏览器已就绪（headless=%s）", self.headless)

            while not self._stop.is_set():
                self._apply_commands(context, pages)
                self._apply_retries(context, pages)
                self._check_silent()
                keeper.wait_for_timeout(200)     # 必须用它来泵事件
        finally:
            for page in pages.values():
                _quietly(page.close)
            _quietly(context.close)
            if browser is not None:
                _quietly(browser.close)

    def _apply_commands(self, context, pages: Dict[str, object]) -> None:
        while True:
            try:
                action, rid = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "open":
                    self._open_page(context, pages, rid)
                elif action == "close_page":
                    page = pages.pop(rid, None)
                    if page is not None:
                        _quietly(page.close)
                        log.info("[%s] 已关闭页面（剩 %d 个）", rid, len(pages))
            except Exception as exc:            # noqa: BLE001 - 单个房间失败不能拖垮 hub
                log.warning("[%s] %s 失败：%s", rid, action, str(exc).splitlines()[0])
                if action == "open":
                    self._schedule_retry(rid)

    def _schedule_retry(self, rid: str) -> None:
        if self._channel(rid) is None:
            return                              # 已经被移除了，不用管
        count = self._retries.get(rid, 0) + 1
        if count > PAGE_RETRY_LIMIT:
            log.error("[%s] 页面连续 %d 次打不开，这个房间将没有弹幕", rid, count - 1)
            return
        self._retries[rid] = count
        self._pending[rid] = time.time() + PAGE_RETRY_DELAY
        log.info("[%s] %.0f 秒后重开页面（第 %d 次）", rid, PAGE_RETRY_DELAY, count)

    def _check_silent(self) -> None:
        """页面开着、却迟迟没挂上弹幕 WebSocket 的房间，喊一嗓子。

        比只认「验证码」三个字更耐用：不管抖音以后换成什么拦法，只要弹幕没
        连上就会报出来，不会再出现录完一整场才发现 jsonl 是空的。
        """
        now = time.time()
        with self._rooms_lock:
            channels = list(self._rooms.values())
        for ch in channels:
            if ch.warned or ch.connected.is_set():
                continue
            if now - ch.attached_at < WS_CONNECT_TIMEOUT:
                continue
            ch.warned = True
            log.error("[%s] %.0f 秒内没能挂上弹幕 WebSocket，这场大概率一条弹幕都收不到。%s",
                      ch.web_rid, WS_CONNECT_TIMEOUT,
                      BLOCKED_ADVICE if ch.blocked else
                      "画面不受影响，仍在正常录制。")

    def _apply_retries(self, context, pages: Dict[str, object]) -> None:
        if not self._pending:
            return
        now = time.time()
        for rid in [r for r, due in self._pending.items() if due <= now]:
            self._pending.pop(rid, None)
            self._commands.put(("open", rid))

    def _open_page(self, context, pages: Dict[str, object], rid: str) -> None:
        channel = self._channel(rid)
        if channel is None:
            return                              # 还没打开就被移除了
        if rid in pages:
            _quietly(pages.pop(rid).close)

        page = context.new_page()
        pages[rid] = page
        channel.page = page
        page.on("websocket", lambda ws: self._on_websocket(channel, ws))
        page.on("response", lambda resp: self._on_response(channel, resp))

        url = "https://live.douyin.com/%s" % rid
        log.info("[%s] 打开直播间页面（当前 %d 个）", rid, len(pages))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._retries.pop(rid, None)

        # goto 成功不等于进了直播间：被风控挡住时返回的是验证页，HTTP 也是 200
        blocked = _verify_page(page)
        channel.blocked = blocked
        if blocked:
            log.error("[%s] 被抖音风控挡在验证页（%s），本场收不到弹幕。%s",
                      rid, blocked, BLOCKED_ADVICE)

    # -- Playwright 回调 --------------------------------------------------

    def _on_websocket(self, channel: RoomChannel, ws) -> None:
        if IM_PATH not in ws.url:
            return
        log.info("[%s] 已挂上弹幕 WebSocket", channel.web_rid)
        channel.connected.set()
        ws.on("framereceived", lambda payload: self._on_frame(channel, payload))
        ws.on("close", lambda _=None: log.info("[%s] 弹幕连接断开（页面会自己重连）",
                                               channel.web_rid))

    def _on_frame(self, channel: RoomChannel, payload) -> None:
        if not isinstance(payload, (bytes, bytearray)):
            return
        channel.frames += 1
        try:
            self._frames.put_nowait((channel, bytes(payload)))
        except queue.Full:
            log.warning("弹幕解码跟不上，丢弃一帧")

    def _on_response(self, channel: RoomChannel, resp) -> None:
        if ENTER_PATH not in resp.url or channel.on_room_info is None:
            return
        try:
            info = room.from_enter_payload(resp.json(), channel.web_rid)
        except Exception as exc:                # noqa: BLE001
            log.debug("[%s] 读取 enter 响应失败：%s", channel.web_rid, exc)
            return
        if info:
            log.info("[%s] 从页面拿到直播间信息：%s", channel.web_rid, info.describe())
            channel.on_room_info(info)


BLOCKED_ADVICE = ("到「设置 → 显示浏览器窗口」打开有头模式，在弹出的窗口里"
                  "自己过一次验证；配合「保持登录态」，验证结果会留在本地 "
                  "profile 里，之后不用每次都做。")


def _verify_page(page) -> str:
    """判断当前页面是不是风控的验证中间页，是就返回标题（或 URL）。"""
    try:
        title = page.title() or ""
        url = page.url or ""
    except Exception:                           # noqa: BLE001 - 页面可能已经关了
        return ""
    blob = (title + " " + url).lower()
    if any(hint in blob for hint in VERIFY_HINTS):
        return title or url
    return ""


def _quietly(fn) -> None:
    try:
        fn()
    except Exception:                           # noqa: BLE001 - 收尾失败无所谓
        pass


def _block_heavy(route) -> None:
    """拦掉直播流和图片等重资源 —— 画面由 ffmpeg 单独拉，浏览器不需要解码。"""
    req = route.request
    if any(h in req.url for h in STREAM_HINTS) or req.resource_type in BLOCKED_TYPES:
        route.abort()
    else:
        route.continue_()


def _parse_cookies(cookie: str) -> List[dict]:
    out = []
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        out.append({"name": name, "value": value, "domain": ".douyin.com", "path": "/"})
    return out
