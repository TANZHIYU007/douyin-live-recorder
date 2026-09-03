"""弹幕落盘。

JSONL 是唯一的完整记录（所有字段都在），XML 只是为了能直接喂给播放器，
ASS 交给 tools/convert.py 事后生成 —— 录制时少做一件事，少一个崩溃点。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional
from xml.sax.saxutils import escape

from .messages import Event

log = logging.getLogger("writer")

XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<i>\n"
    "  <chatserver>live.douyin.com</chatserver>\n"
    "  <chatid>{room}</chatid>\n"
    "  <mission>0</mission>\n"
    "  <maxlimit>2147483647</maxlimit>\n"
    "  <state>0</state>\n"
    "  <real_name>0</real_name>\n"
    "  <source>Lumina</source>\n"
)


class Writer:
    def write(self, ev: Event) -> None: ...
    def close(self) -> None: ...


class JsonlWriter(Writer):
    """一行一个事件，字段最全，其他格式都从这里转换。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8", newline="\n")
        self.count = 0
        self._last_flush = time.time()

    def write(self, ev: Event) -> None:
        self._fh.write(ev.to_json() + "\n")
        self.count += 1
        # 每秒最多刷一次盘：中途断电时最多丢 1 秒弹幕，又不会拖慢高频房间
        now = time.time()
        if now - self._last_flush >= 1.0:
            self._fh.flush()
            self._last_flush = now

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except OSError as exc:
            log.warning("关闭 %s 失败：%s", self.path.name, exc)


class XmlWriter(Writer):
    """B 站弹幕 XML，potplayer / 各类弹幕播放器可直接加载。"""

    def __init__(self, path: Path, room: str = "0"):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8", newline="\n")
        self._fh.write(XML_HEADER.format(room=escape(room)))
        self.count = 0

    def write(self, ev: Event) -> None:
        if ev.kind not in ("chat", "emoji"):
            return
        # p = 出现秒数,模式(1滚动),字号,颜色,发送时间戳,弹幕池,发送者hash,弹幕id
        attr = "%.3f,1,25,16777215,%d,0,%s,%s" % (
            max(0.0, ev.offset), int(ev.ts),
            ev.user_id or "0", ev.extra.get("msg_id", "0"))
        self._fh.write('  <d p="%s">%s</d>\n' % (attr, escape(ev.content)))
        self.count += 1

    def close(self) -> None:
        try:
            self._fh.write("</i>\n")
            self._fh.close()
        except OSError as exc:
            log.warning("关闭 %s 失败：%s", self.path.name, exc)


class ConsoleWriter(Writer):
    """把弹幕实时打到终端，方便确认真的在录。"""

    PREFIX = {"chat": "💬", "emoji": "😀", "gift": "🎁", "member": "🚪",
              "social": "❤️", "like": "👍", "control": "⚠️", "fansclub": "🏅"}

    def __init__(self, kinds: Iterable[str] = ("chat", "emoji", "gift", "social")):
        self.kinds = set(kinds)

    def write(self, ev: Event) -> None:
        if ev.kind not in self.kinds:
            return
        icon = self.PREFIX.get(ev.kind, "·")
        name = ev.user_name or "-"
        print("  %s [%s] %s: %s" % (icon, _clock(ev.offset), name, ev.content),
              flush=True)

    def close(self) -> None:
        pass


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


class WriterGroup:
    """一组 writer，带锁 —— 弹幕从网络线程进来，主线程也会调 close。"""

    def __init__(self, writers: Optional[List[Writer]] = None):
        self._writers: List[Writer] = list(writers or [])
        self._lock = threading.Lock()
        self.total = 0

    def add(self, writer: Writer) -> None:
        with self._lock:
            self._writers.append(writer)

    def write(self, ev: Event) -> None:
        with self._lock:
            self.total += 1
            for w in self._writers:
                try:
                    w.write(ev)
                except Exception as exc:
                    log.warning("%s 写入失败：%s", type(w).__name__, exc)

    def close(self) -> None:
        with self._lock:
            for w in self._writers:
                try:
                    w.close()
                except Exception as exc:
                    log.warning("%s 关闭失败：%s", type(w).__name__, exc)
            self._writers.clear()
