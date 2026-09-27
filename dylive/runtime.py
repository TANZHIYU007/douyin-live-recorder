"""打包成 exe 之后的运行时资源管理。

PyInstaller 单文件模式每次启动都会把打包内容解压到临时目录。Chromium 有几百个
文件、四百多 MB，走这条路冷启动要几十秒，而且每次都往磁盘写一遍。

所以 ffmpeg、ffprobe 和 Chromium 不进 PyInstaller 的归档，而是打成一个 zip **追加在 exe
尾部**：首次运行解压到用户目录，之后每次启动只要确认标记文件还在就直接复用。
exe 依然是单个文件，冷启动只需要解压 Python 和 Qt 那部分。

尾部结构：
    [PyInstaller 的 exe] [payload.zip] [MAGIC(16)] [zip 长度(u64 LE)] [构建号(32)]
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import paths

MAGIC = b"DYLIVEPAYLOAD001"
BUILD_ID_LEN = 32
FOOTER_LEN = len(MAGIC) + 8 + BUILD_ID_LEN

# zip 内部的固定布局
BROWSERS_DIR = "ms-playwright"
FFMPEG_REL = "bin/ffmpeg" + paths.EXE_SUFFIX
FFPROBE_REL = "bin/ffprobe" + paths.EXE_SUFFIX

_STAMP = ".build_id"
_LOCK_FILE = ".runtime.lock"
_LOCK_TIMEOUT = 300.0


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_runtime() -> Optional[Path]:
    """随程序一起装好、不用解压的运行时目录，没有则 None。

    追加到可执行文件尾部那套是 Windows 单文件 exe 专用的。macOS 的 .app
    本来就是个目录，把 Chromium、ffmpeg 和 ffprobe 放进 Contents/Resources/runtime
    就行，连解压这一步都省了；而且在 macOS 上往可执行文件屁股后面加字节会
    **破坏代码签名**，Apple 芯片上签名一坏程序直接起不来。
    """
    if not is_frozen():
        return None
    base = getattr(sys, "_MEIPASS", "")
    if base:
        candidate = Path(base) / "runtime"
        if candidate.is_dir():
            return candidate
    return None


def runtime_dir() -> Path:
    """运行时在哪。随程序装好的优先，否则用解压出来的那份。"""
    packed = bundled_runtime()
    if packed is not None:
        return packed
    return paths.app_data_dir() / "runtime"


def read_footer() -> Optional[Tuple[int, int, str]]:
    """读 exe 尾部的载荷信息，返回 (偏移, 长度, 构建号)。没有则 None。"""
    if not is_frozen():
        return None
    exe = Path(sys.executable)
    try:
        total = exe.stat().st_size
        if total < FOOTER_LEN:
            return None
        with exe.open("rb") as fh:
            fh.seek(total - FOOTER_LEN)
            footer = fh.read(FOOTER_LEN)
    except OSError:
        return None

    if not footer.startswith(MAGIC):
        return None
    size = int.from_bytes(footer[len(MAGIC):len(MAGIC) + 8], "little")
    build_id = footer[len(MAGIC) + 8:].decode("ascii", "replace")
    offset = total - FOOTER_LEN - size
    if offset < 0:
        return None
    return offset, size, build_id


def is_ready() -> bool:
    """运行时是否已经就位，且和当前程序是同一个构建。"""
    if bundled_runtime() is not None:
        return True                     # 跟程序一起装好的，不存在没准备好
    info = read_footer()
    if info is None:
        return True                     # 源码运行，不需要这套
    stamp = runtime_dir() / _STAMP
    try:
        if stamp.read_text(encoding="ascii").strip() != info[2]:
            return False
    except OSError:
        return False
    target = runtime_dir()
    browsers = target / BROWSERS_DIR
    chrome = next(browsers.glob("chromium-*/chrome-win*/chrome.exe"), None)
    return (chrome is not None
            and (target / FFMPEG_REL).is_file()
            and (target / FFPROBE_REL).is_file())


@contextmanager
def _runtime_lock(progress: Optional[Callable[[int, int, str], None]] = None):
    """跨进程串行化首次解压；进程异常退出时系统会自动释放文件锁。"""
    root = paths.app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_FILE
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()

        deadline = time.monotonic() + _LOCK_TIMEOUT
        waiting_reported = False
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "等待另一个拾光完成运行环境准备超时，请关闭其他拾光窗口后重试"
                    ) from exc
                if progress is not None and not waiting_reported:
                    progress(0, 1, "正在等待另一个拾光完成运行环境准备…")
                    waiting_reported = True
                time.sleep(0.2)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class _Slice:
    """把 exe 里的一段字节伪装成可 seek 的文件对象。

    载荷有两三百 MB，整块读进内存没必要，zipfile 只需要能 seek/read。
    """

    def __init__(self, path: Path, offset: int, size: int):
        self._fh = path.open("rb")
        self._offset = offset
        self._size = size
        self._pos = 0

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 1:
            pos += self._pos
        elif whence == 2:
            pos += self._size
        self._pos = max(0, min(pos, self._size))
        self._fh.seek(self._offset + self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        left = self._size - self._pos
        n = left if n is None or n < 0 else min(n, left)
        data = self._fh.read(n)
        self._pos += len(data)
        return data

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def extract(progress: Optional[Callable[[int, int, str], None]] = None) -> None:
    """把尾部载荷解压到运行时目录。progress(已完成, 总数, 当前文件名)。"""
    if bundled_runtime() is not None:
        return                          # 已经在包里了，没什么可解压的
    info = read_footer()
    if info is None:
        return
    offset, size, build_id = info
    if is_ready():
        return

    # 两次快速双击会同时走到这里。文件锁让第二个进程等待，第一个完成后
    # 再检查一次并直接复用，避免双方互删文件。
    with _runtime_lock(progress):
        if is_ready():
            return

        target = runtime_dir()
        staging = target.parent / (".runtime-preparing-%d-%s"
                                   % (os.getpid(), uuid.uuid4().hex))
        staging.mkdir(parents=True, exist_ok=False)
        try:
            with _Slice(Path(sys.executable), offset, size) as blob:
                with zipfile.ZipFile(blob) as zf:
                    members = zf.infolist()
                    total = sum(m.file_size for m in members) or 1
                    done = 0
                    for member in members:
                        zf.extract(member, staging)
                        done += member.file_size
                        if progress is not None:
                            progress(done, total, member.filename)

            # 标记最后写进临时目录，完整解压后才替换正式目录。中断时用户
            # 要么仍有旧环境，要么没有环境，绝不会看到“带标记的半成品”。
            (staging / _STAMP).write_text(build_id, encoding="ascii")
            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def apply_env() -> None:
    """让 Playwright 去内置目录里找浏览器。必须在启动引擎之前调用。"""
    if not is_frozen():
        return
    browsers = runtime_dir() / BROWSERS_DIR
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)


def browser_channel() -> str:
    """完整版用内置 Chromium；轻量版优先复用系统 Edge/Chrome。"""
    if bundled_runtime() is not None or read_footer() is not None:
        return "chromium"
    if paths.WINDOWS and is_frozen():
        candidates = (
            ("msedge", os.environ.get("PROGRAMFILES(X86)", ""),
             "Microsoft/Edge/Application/msedge.exe"),
            ("msedge", os.environ.get("PROGRAMFILES", ""),
             "Microsoft/Edge/Application/msedge.exe"),
            ("msedge", os.environ.get("LOCALAPPDATA", ""),
             "Microsoft/Edge/Application/msedge.exe"),
            ("chrome", os.environ.get("PROGRAMFILES", ""),
             "Google/Chrome/Application/chrome.exe"),
            ("chrome", os.environ.get("PROGRAMFILES(X86)", ""),
             "Google/Chrome/Application/chrome.exe"),
            ("chrome", os.environ.get("LOCALAPPDATA", ""),
             "Google/Chrome/Application/chrome.exe"),
        )
        for channel, root, relative in candidates:
            if root and (Path(root) / relative).is_file():
                return channel
    # 源码环境和自行安装过 Playwright Chromium 的轻量版仍可走原逻辑。
    return "chromium"


def bundled_ffmpeg() -> str:
    """内置 ffmpeg 的路径，没有就返回空串（回落到系统 PATH）。

    只在打包运行时生效 —— 否则源码调试会悄悄用上某次打包留下的缓存副本。
    """
    if not is_frozen():
        return ""
    path = runtime_dir() / FFMPEG_REL
    return str(path) if path.is_file() else ""


def bundled_ffprobe() -> str:
    """内置 ffprobe 的路径，没有就返回空串。"""
    if not is_frozen():
        return ""
    path = runtime_dir() / FFPROBE_REL
    return str(path) if path.is_file() else ""


def default_output_dir() -> Path:
    """默认输出目录。

    打包版可能从任意位置被双击启动，用相对的 output/ 会把录像丢到莫名其妙的
    地方（甚至系统目录），所以固定放到用户的「视频」文件夹下。
    """
    if not is_frozen():
        return Path("output").resolve()
    return paths.videos_dir() / "拾光录制"


def describe() -> str:
    if bundled_runtime() is not None:
        return "内置运行时（随程序分发），位于 %s" % runtime_dir()
    info = read_footer()
    if info is None:
        if is_frozen():
            return "轻量版（使用系统 ffmpeg 与 %s）" % browser_channel()
        return "源码运行（使用系统 ffmpeg 与 Playwright 浏览器）"
    return "内置运行时 %s，位于 %s" % (info[2][:8], runtime_dir())
