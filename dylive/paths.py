"""各操作系统的差异都收在这里。

底下那套东西（Playwright、ffmpeg、PySide6、requests）三个平台都有，真正
挡路的只是「文件放哪、字体叫什么、怎么打开文件夹」这类零碎。散在各处的
话每加一个平台就得满仓库找一遍，所以集中到这一个模块。

不要在别处写 sys.platform 判断，加到这里来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

APP_DIR_NAME = "Lumina"

# GUI 版没有控制台，Windows 上直接起子进程会闪一个黑框出来；其他系统没这问题
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}

EXE_SUFFIX = ".exe" if WINDOWS else ""


def app_data_dir() -> Path:
    """配置、日志、解压出来的运行时都放这儿，各平台按各自的规矩来。"""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / APP_DIR_NAME
    if MACOS:
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def videos_dir() -> Path:
    """系统的「视频」文件夹。macOS 叫「影片」（Movies），不是 Videos。"""
    home = Path.home()
    candidates = [home / "Movies"] if MACOS else [home / "Videos"]
    for path in candidates:
        if path.is_dir():
            return path
    return home


def ui_font() -> str:
    """界面正文字体。挑各系统自带的中文无衬线，免得回落到难看的宋体。"""
    if WINDOWS:
        return "Microsoft YaHei UI"
    if MACOS:
        return "PingFang SC"
    return "Noto Sans CJK SC"


def subtitle_font() -> str:
    """写进 ASS 的字体名。字幕是播放时才渲染的，得用播放机器上真有的字体。"""
    if WINDOWS:
        return "微软雅黑"
    if MACOS:
        return "PingFang SC"
    return "Noto Sans CJK SC"


def open_in_file_manager(path: Path) -> None:
    """在系统的文件管理器里打开一个目录。"""
    target = str(path)
    if WINDOWS:
        os.startfile(target)                                # noqa: S606
    elif MACOS:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])
