"""通用小工具：日志、文件名清洗、时间格式化。"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ILLEGAL = re.compile(r'[\/:*?"<>|\r\n\t]+')


def setup_console() -> None:
    """Windows 控制台默认 GBK，直播间标题里的表情会直接抛异常，强制切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(level: str = "INFO", logfile: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # websocket-client 会把整个握手响应头 dump 到 ERROR，我们自己有更好的提示
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def safe_name(text: str, limit: int = 60) -> str:
    """把直播间标题变成能落盘的文件名片段。"""
    text = _ILLEGAL.sub("_", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:limit] or "untitled"


def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))


def hms(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d.%02d" % (h, m, s, int(seconds % 1 * 100))
