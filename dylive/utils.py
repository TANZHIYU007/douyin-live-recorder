"""通用小工具：日志、文件名清洗、时间格式化。"""

from __future__ import annotations

import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 反斜杠也必须洗掉：Windows 上它是路径分隔符，留着会让录像落到一层
# 子目录里去，而找产出文件是按「同目录 + 前缀」匹配的 —— 文件找不着，
# 弹幕字幕就会一声不响地什么都没做。
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


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
    quiet_noisy_loggers()


def log_dir() -> Path:
    from . import paths                  # 延迟导入：utils 被很早就 import 进来
    return paths.app_data_dir() / "logs"


def add_rotating_file_log(verbose: bool = False, max_bytes: int = 2 << 20,
                          backups: int = 5) -> Path | None:
    """给根 logger 挂一个滚动文件处理器，返回日志文件路径；失败返回 None。

    图形界面没有控制台，日志原本只存在内存里 —— 窗口一关就什么都不剩。
    可出问题的时候恰恰是事后才想起来要看：「弹幕怎么一条都没有」这种问题，
    没有落盘的日志就只能靠复现。

    verbose 打开时文件里记到 DEBUG（界面上仍然只显示 INFO 及以上）——
    排查弹幕为什么连不上时，有用的全在 DEBUG 里。
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, "_lumina_file_log", False):
            existing.setLevel(logging.DEBUG if verbose else logging.INFO)
            return Path(getattr(existing, "baseFilename", "")) or None

    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "lumina.log"
        handler = RotatingFileHandler(path, maxBytes=max_bytes,
                                      backupCount=backups, encoding="utf-8")
    except OSError as exc:
        logging.getLogger("utils").warning("日志文件建不起来（%s），只记在内存里", exc)
        return None

    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"))
    handler._lumina_file_log = True      # 下次调用认这个标记，不会挂两份
    root.addHandler(handler)
    return path


def quiet_noisy_loggers() -> None:
    """把第三方库里没营养的日志压下去。

    根 logger 放到 DEBUG 之后，这些库会把握手细节、每个 HTTP 连接、
    asyncio 的调度统统倒出来，真正有用的信息会被淹掉。
    """
    # websocket-client 会把整个握手响应头 dump 到 ERROR，我们自己有更好的提示
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    for name in ("urllib3", "asyncio", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


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
