"""多房间调度。

每个房间一个 Recorder、一个线程，各录各的；但**所有房间共用一个 Chromium**
（见 browser_hub.py），否则五个房间就是一点几个 G 的内存。

界面只跟这里打交道：加房间、开始/停止、取状态、取弹幕。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from typing import Callable, Dict, List, Optional

from . import room as room_mod
from .browser_hub import BrowserHub
from .recorder import Options, Recorder, Status
from .writers import Writer

log = logging.getLogger("manager")

IDLE, STARTING, WAITING, RECORDING, PROCESSING, ERROR = (
    "idle", "starting", "waiting", "recording", "processing", "error")

STATE_LABELS = {
    IDLE: "未开始", STARTING: "启动中", WAITING: "守候中",
    RECORDING: "录制中", PROCESSING: "处理中", ERROR: "出错",
}


class RoomBuffer(Writer):
    """每个房间自己的弹幕缓冲。界面按批取走，取不完的丢最旧的。"""

    def __init__(self, limit: int = 20000):
        self.events: deque = deque(maxlen=limit)
        self._lock = threading.Lock()

    def write(self, ev) -> None:
        with self._lock:
            self.events.append(ev)

    def drain(self, limit: int = 400) -> List:
        with self._lock:
            return [self.events.popleft()
                    for _ in range(min(limit, len(self.events)))]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def close(self) -> None:
        pass                        # 界面持有，跨场次复用


class Room:
    """监测列表里的一个房间。"""

    def __init__(self, web_rid: str, hub: BrowserHub,
                 make_options: Callable[[str], Options]):
        self.web_rid = web_rid
        self.hub = hub
        self.make_options = make_options

        self.info: Optional[room_mod.RoomInfo] = None
        self.buffer = RoomBuffer()
        self.error = ""
        self.totals: Counter = Counter()        # 跨场次累计
        self.sessions = 0

        self._recorder: Optional[Recorder] = None
        self._thread: Optional[threading.Thread] = None
        self._starting = False
        # 停止请求记在房间上，而不是只转发给 recorder：Recorder 构造要做几秒
        # 网络请求，那段时间 _recorder 还是 None，这时点停止会被静默丢掉。
        self._stop_requested = threading.Event()

    # -- 状态 -------------------------------------------------------------

    @property
    def state(self) -> str:
        if self.error:
            return ERROR
        if self._thread is None or not self._thread.is_alive():
            return IDLE
        rec = self._recorder
        if rec is None:
            return STARTING
        st = rec.status()
        if st.running:
            return RECORDING
        return PROCESSING if st.post else WAITING

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> Status:
        rec = self._recorder
        return rec.status() if rec is not None else Status()

    def display_name(self) -> str:
        if self.info and self.info.nickname:
            return self.info.nickname
        return self.web_rid

    # -- 启停 -------------------------------------------------------------

    def start(self) -> None:
        if self.active or self._starting:
            return
        self.error = ""
        self._stop_requested.clear()
        self._starting = True
        self._thread = threading.Thread(target=self._run, name="room-" + self.web_rid,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        rec = self._recorder
        if rec is not None:
            rec.stop()

    def join(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            opts = self.make_options(self.web_rid)
            recorder = Recorder(opts, hub=self.hub)
            recorder.extra_writers.append(self.buffer)
            self._recorder = recorder
            self._starting = False
            if self._stop_requested.is_set():
                recorder.stop()         # 构造期间被叫停了，别再开录
            recorder.run()
        except Exception as exc:                # noqa: BLE001 - 报给界面
            self.error = str(exc)
            log.error("[%s] 录制线程异常：%s", self.web_rid, exc)
        finally:
            self._starting = False
            rec = self._recorder
            if rec is not None:
                self.totals.update(rec.status().counts)
            self._recorder = None


class RoomManager:
    def __init__(self, make_options: Callable[[str], Options],
                 headless: bool = True, user_data_dir: str = "",
                 cookie: str = "", proxy: str = ""):
        self.make_options = make_options
        self.hub = BrowserHub(headless=headless, user_data_dir=user_data_dir,
                              cookie=cookie, proxy=proxy)
        self._rooms: Dict[str, Room] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()

    # -- 增删 -------------------------------------------------------------

    def add(self, web_rid: str) -> Room:
        with self._lock:
            if web_rid in self._rooms:
                return self._rooms[web_rid]
            entry = Room(web_rid, self.hub, self.make_options)
            self._rooms[web_rid] = entry
            self._order.append(web_rid)
            return entry

    def remove(self, web_rid: str) -> None:
        with self._lock:
            entry = self._rooms.pop(web_rid, None)
            if web_rid in self._order:
                self._order.remove(web_rid)
        if entry is not None:
            entry.stop()

    def get(self, web_rid: str) -> Optional[Room]:
        with self._lock:
            return self._rooms.get(web_rid)

    def rooms(self) -> List[Room]:
        with self._lock:
            return [self._rooms[r] for r in self._order if r in self._rooms]

    # -- 批量 -------------------------------------------------------------

    def start_all(self) -> int:
        started = 0
        for entry in self.rooms():
            if not entry.active:
                entry.start()
                started += 1
        return started

    def stop_all(self) -> None:
        for entry in self.rooms():
            entry.stop()

    @property
    def recording_count(self) -> int:
        return sum(1 for r in self.rooms() if r.state == RECORDING)

    @property
    def active_count(self) -> int:
        return sum(1 for r in self.rooms() if r.active)

    def total_bytes(self) -> int:
        return sum(r.status().total_bytes for r in self.rooms())

    def shutdown(self, timeout: float = 30.0) -> None:
        """停掉所有房间，等它们收尾，最后关掉共享浏览器。"""
        self.stop_all()
        deadline = time.time() + timeout
        for entry in self.rooms():
            entry.join(max(1.0, deadline - time.time()))
        self.hub.stop()
