"""单房间的浏览器引擎。

真正的实现在 browser_hub.py —— 那边一个 Chromium 能带多个房间。这里只是把它
包成「一个引擎对应一个房间」的形状，给命令行版和只录一个房间的场景用，
免得同一件事有两份实现。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from . import room
from .browser_hub import BrowserHub, RoomChannel
from .messages import Event

log = logging.getLogger("danmaku.browser")


class BrowserEngine:
    name = "browser"

    def __init__(
        self,
        web_rid: str,
        on_event: Callable[[Event], None],
        on_room_info: Optional[Callable[[room.RoomInfo], None]] = None,
        headless: bool = True,
        block_media: bool = True,
        user_data_dir: Optional[str] = None,
        cookie: str = "",
        proxy: str = "",
    ):
        self.web_rid = web_rid
        self.on_event = on_event
        self.on_room_info = on_room_info
        self.start_time = time.time()

        self._hub = BrowserHub(headless=headless, block_media=block_media,
                               user_data_dir=user_data_dir or "",
                               cookie=cookie, proxy=proxy)
        self._channel: Optional[RoomChannel] = None

    def start(self) -> None:
        self._hub.start()
        self._channel = self._hub.attach(self.web_rid, self.on_event, self.on_room_info)
        self._channel.start_time = self.start_time

    def stop(self) -> None:
        self._hub.stop()

    def wait_connected(self, timeout: float = 45.0) -> bool:
        return self._channel.wait_connected(timeout) if self._channel else False

    @property
    def frames(self) -> int:
        return self._channel.frames if self._channel else 0

    @property
    def connected(self):
        return self._channel.connected if self._channel else None

    @property
    def failed(self) -> Optional[BaseException]:
        return self._hub.failed
