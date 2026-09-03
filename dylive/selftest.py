"""环境自检。

打包版没有控制台，用户遇到「录不了」时没法看日志。跑一遍自检能一次性确认
运行时、ffmpeg、浏览器、网络这四块哪块出了问题。

    Lumina.exe --selftest
    python gui.py --selftest
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

from . import paths, runtime
from .utils import UA

TIMEOUT = 60


def _report_path() -> Path:
    return paths.app_data_dir() / "selftest.txt"


def _check_runtime() -> Tuple[bool, str]:
    if not runtime.is_frozen():
        return True, "源码运行，使用系统 ffmpeg 与 Playwright 浏览器"
    if not runtime.is_ready():
        return False, "内置运行时尚未解压（正常启动一次即可）"
    target = runtime.runtime_dir()
    count = sum(1 for _ in target.rglob("*"))
    return True, "已解压到 %s（%d 个条目）" % (target, count)


def _check_ffmpeg() -> Tuple[bool, str]:
    from . import video
    try:
        path = video.find_ffmpeg()
    except RuntimeError as exc:
        return False, str(exc)
    try:
        out = subprocess.run([path, "-version"], capture_output=True, timeout=20,
                             **video.NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "%s 无法执行：%s" % (path, exc)
    first = out.stdout.decode("utf-8", "replace").splitlines()
    return out.returncode == 0, (first[0] if first else "无输出") + "\n    " + path


def _check_browser() -> Tuple[bool, str]:
    runtime.apply_env()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return False, "playwright 导入失败：%s" % exc

    started = time.time()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium", headless=True,
                                         args=["--mute-audio"])
            page = browser.new_page()
            page.goto("about:blank")
            version = browser.version
            browser.close()
    except Exception as exc:            # noqa: BLE001 - 结果要写进报告
        return False, "启动 Chromium 失败：%s" % str(exc)[:400]
    return True, "Chromium %s 启动正常（%.1f 秒）；浏览器目录 %s" % (
        version, time.time() - started,
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "系统默认"))


def _check_network() -> Tuple[bool, str]:
    try:
        import requests
        resp = requests.get("https://live.douyin.com/", timeout=15,
                            headers={"User-Agent": UA})
    except Exception as exc:            # noqa: BLE001
        return False, "访问 live.douyin.com 失败：%s" % str(exc)[:200]
    cookies = [c.name for c in resp.cookies]
    ok = resp.status_code == 200 and "ttwid" in cookies
    return ok, "HTTP %d，拿到 cookie %s" % (resp.status_code, cookies or "无")


CHECKS = [
    ("运行时", _check_runtime),
    ("ffmpeg", _check_ffmpeg),
    ("浏览器", _check_browser),
    ("网络", _check_network),
]


def run() -> Tuple[bool, str]:
    """跑全部检查，返回 (是否全过, 报告文本)。"""
    lines: List[str] = [
        "拾光 Lumina 环境自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "打包运行: %s" % runtime.is_frozen(),
        "可执行文件: %s" % sys.executable,
        "-" * 60,
    ]
    all_ok = True
    for name, check in CHECKS:
        try:
            ok, detail = check()
        except Exception as exc:        # noqa: BLE001 - 自检本身不能崩
            ok, detail = False, "检查过程异常：%r" % exc
        all_ok &= ok
        lines.append("[%s] %-8s %s" % ("通过" if ok else "失败", name, detail))
    lines.append("-" * 60)
    lines.append("结论：%s" % ("全部通过，可以正常录制" if all_ok else "存在问题，见上"))

    text = "\n".join(lines)
    try:
        path = _report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        text += "\n\n报告已保存到 %s" % path
    except OSError:
        pass
    return all_ok, text
