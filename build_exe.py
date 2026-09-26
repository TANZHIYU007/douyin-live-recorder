#!/usr/bin/env python
"""把「拾光 Lumina」打成一个单文件 exe。

    python build_exe.py

分两步：
  1. PyInstaller 打出只含 Python + Qt + playwright 驱动的单文件 exe（约 100 MB）；
  2. 把 Chromium、ffmpeg 和 ffprobe 压成 zip，**追加到 exe 尾部**。

第二步是关键。如果把浏览器交给 PyInstaller 打包，单文件模式每次启动都要把
四百多 MB、几百个文件解压到临时目录，冷启动要几十秒。追加在尾部的话
PyInstaller 不认识这段数据、不会碰它，程序首次运行自己解压到用户目录，
之后每次启动只解压 Qt 那一小部分。

产物：dist/Lumina.exe
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "Lumina"

MAGIC = b"DYLIVEPAYLOAD001"

# 用不到的 Qt 模块，排掉能省下一大截
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
]
OTHER_EXCLUDES = ["tkinter", "unittest", "pydoc_data", "test", "PIL", "numpy",
                  "matplotlib", "pandas", "IPython", "setuptools", "pip"]


def log(msg: str) -> None:
    print("[build] " + msg, flush=True)


def human(num: float) -> str:
    return "%.1f MB" % (num / 1048576) if num < 1 << 30 else "%.2f GB" % (num / (1 << 30))


def _is_below(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def sanitized_build_path(path_value: str, system_root: Path) -> tuple[str, list[str]]:
    """把会劫持 Qt 系统 ICU 的第三方目录从打包 PATH 中拿掉。

    Qt6Core 在 Windows 上依赖系统的 ``icuuc.dll``。如果打包机 PATH 里另有
    Poppler/Conda 等自带的同名 DLL，PyInstaller 会误把它连同其私有 ICU 数据
    一起塞进 exe。运行时这个副本优先于 System32，QtCore 随即导入失败。
    """
    kept: list[str] = []
    removed: list[str] = []
    for raw in path_value.split(os.pathsep):
        if not raw:
            continue
        candidate = Path(raw.strip('"'))
        if ((candidate / "icuuc.dll").is_file()
                and not _is_below(candidate, system_root)):
            removed.append(raw)
            continue
        kept.append(raw)
    return os.pathsep.join(kept), removed


def pyinstaller_env() -> dict[str, str]:
    """给 PyInstaller 一份不受开发工具私有 DLL 污染的环境。"""
    env = os.environ.copy()
    if sys.platform != "win32":
        return env
    system_root = Path(env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows")
    env["PATH"], removed = sanitized_build_path(env.get("PATH", ""), system_root)
    for item in removed:
        log("打包 PATH 已排除第三方 ICU 目录：%s" % item)
    return env


_ROOT_ICU = re.compile(r"icu(?:dt|in|io|test|tu|uc)\d*\.dll", re.IGNORECASE)


def unexpected_root_icu(names) -> list[str]:
    """返回被错误打进 PyInstaller 根目录的 ICU DLL。"""
    return sorted(
        name for name in names
        if PureWindowsPath(name).parent == PureWindowsPath(".")
        and _ROOT_ICU.fullmatch(PureWindowsPath(name).name)
    )


def verify_pyinstaller_archive(exe: Path) -> None:
    """在追加浏览器载荷前检查 Qt 归档，阻止已知坏包流出。"""
    from PyInstaller.archive.readers import CArchiveReader

    names = list(CArchiveReader(str(exe)).toc)
    required = {"PySide6\\QtCore.pyd", "PySide6\\Qt6Core.dll"}
    missing = sorted(required.difference(names))
    if missing:
        raise SystemExit("PyInstaller 产物缺少 Qt 核心文件：%s" % ", ".join(missing))
    bad = unexpected_root_icu(names)
    if bad:
        raise SystemExit(
            "PyInstaller 错误收进了第三方 ICU DLL：%s。请检查打包 PATH。"
            % ", ".join(bad))
    log("Qt 归档校验通过：QtCore 完整，未混入第三方 ICU")


# --------------------------------------------------------------------------
# 依赖定位
# --------------------------------------------------------------------------

def playwright_root() -> Path:
    """Playwright 把浏览器装在哪。三个系统各不一样。"""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if base:
        return Path(base)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def expected_chromium_revision() -> str:
    """读取当前 Playwright 驱动要求的 Chromium 修订号。"""
    try:
        import playwright
        manifest = (Path(playwright.__file__).resolve().parent
                    / "driver" / "package" / "browsers.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return next(str(item["revision"]) for item in data["browsers"]
                    if item.get("name") == "chromium")
    except (ImportError, OSError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SystemExit("读不到 Playwright 的 Chromium 版本清单：%s" % exc) from exc


def find_chromium() -> Path:
    """找与当前 Playwright 驱动精确匹配的完整版 Chromium。"""
    root = playwright_root()
    if not root.is_dir():
        raise SystemExit("找不到 Playwright 浏览器目录：%s\n"
                         "先执行：python -m playwright install chromium" % root)
    # 不能简单挑目录名最大的版本：Playwright 驱动和浏览器必须成对，旧版目录
    # 即使有 chrome.exe 也启动不了。headless shell 也不能代替完整版。
    expected = root / ("chromium-" + expected_chromium_revision())
    if not expected.is_dir():
        raise SystemExit("当前 Playwright 需要 %s，但本机没有这个 Chromium。\n"
                         "请执行：%s -m playwright install chromium"
                         % (expected.name, sys.executable))
    return expected


def find_ffmpeg() -> Path:
    path = shutil.which("ffmpeg")
    if not path:
        raise SystemExit("PATH 里找不到 ffmpeg.exe。装一个再来："
                         "winget install Gyan.FFmpeg")
    return Path(path)


def find_ffprobe(ffmpeg: Path) -> Path:
    """ffprobe 必须随包提供，字幕分段、分辨率和成品校验都依赖它。"""
    sibling = ffmpeg.with_name("ffprobe.exe")
    path = sibling if sibling.is_file() else Path(shutil.which("ffprobe") or "")
    if not path.is_file():
        raise SystemExit("PATH 里找不到 ffprobe.exe。请安装完整版 FFmpeg 后再打包。")
    return path


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------

def make_icon() -> Path:
    """画一个和界面里 logo 一致的圆角渐变图标。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient,
                               QPainter, QPixmap)
    from PySide6.QtWidgets import QApplication

    # 得留个引用：QApplication 被回收掉的话后面画图就崩了
    _app = QApplication.instance() or QApplication([])
    BUILD.mkdir(parents=True, exist_ok=True)
    icon_path = BUILD / "icon.ico"

    pixmaps = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#FE2C55"))
        grad.setColorAt(1.0, QColor("#7B2FF7"))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)

        font = QFont("Microsoft YaHei UI", int(size * 0.46))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "拾")
        painter.end()
        pixmaps.append(pix)

    _write_ico(icon_path, pixmaps)
    return icon_path


def _write_ico(ico: Path, pixmaps) -> None:
    """手写一个多尺寸 ICO —— 只为了不引入 Pillow 这个依赖。"""
    from PySide6.QtCore import QBuffer, QByteArray

    entries = []
    for pix in pixmaps:
        # QByteArray 必须留个引用：直接传临时对象会被 GC 掉，Qt 那边就成了野指针
        store = QByteArray()
        buf = QBuffer(store)
        buf.open(QBuffer.WriteOnly)
        pix.save(buf, "PNG")
        buf.close()
        entries.append((pix.width(), bytes(store)))

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    directory, blobs = b"", b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset)
        blobs += data
        offset += len(data)
    ico.write_bytes(header + directory + blobs)


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------

def ensure_unlocked() -> None:
    """exe 还在跑的话 PyInstaller 会以「拒绝访问」失败，提前说清楚。"""
    exe = DIST / (APP_NAME + ".exe")
    if not exe.exists():
        return
    try:
        with exe.open("r+b"):
            pass
    except OSError as exc:
        raise SystemExit(
            "%s 正被占用，无法覆盖。先关掉正在运行的 %s（任务管理器里也看一眼），"
            "再重新打包。" % (exe, APP_NAME + ".exe")) from exc


def run_pyinstaller(icon: Path) -> Path:
    ensure_unlocked()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--windowed", "--name", APP_NAME,
           "--icon", str(icon),
           "--collect-data", "playwright",
           "--collect-binaries", "playwright",
           "--hidden-import", "dylive.ui.window"]
    for mod in QT_EXCLUDES + OTHER_EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(ROOT / "gui.py"))

    log("运行 PyInstaller…（几分钟）")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=pyinstaller_env())
    if proc.returncode != 0:
        raise SystemExit("PyInstaller 失败，退出码 %d" % proc.returncode)

    exe = DIST / (APP_NAME + ".exe")
    if not exe.is_file():
        raise SystemExit("没找到产物 %s" % exe)
    log("PyInstaller 产物：%s" % human(exe.stat().st_size))
    verify_pyinstaller_archive(exe)
    return exe


def build_payload(chromium: Path, ffmpeg: Path, ffprobe: Path) -> Path:
    """把浏览器、ffmpeg 和 ffprobe 压成一个 zip。"""
    payload = BUILD / "payload.zip"
    if payload.exists():
        payload.unlink()

    files = [(p, "ms-playwright/%s/%s" % (chromium.name,
                                          p.relative_to(chromium).as_posix()))
             for p in chromium.rglob("*") if p.is_file()]
    files.append((ffmpeg, "bin/ffmpeg.exe"))
    files.append((ffprobe, "bin/ffprobe.exe"))
    raw = sum(p.stat().st_size for p, _ in files)
    log("载荷共 %d 个文件、%s，正在压缩…" % (len(files), human(raw)))

    started = time.time()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, (src, name) in enumerate(files, 1):
            zf.write(src, name)
            if i % 200 == 0 or i == len(files):
                log("  %d/%d" % (i, len(files)))
    log("压缩完成：%s（耗时 %.0f 秒）"
        % (human(payload.stat().st_size), time.time() - started))
    return payload


def append_payload(exe: Path, payload: Path) -> None:
    """把 zip 接到 exe 后面，再补一个定长尾部记录长度和构建号。

    PyInstaller 的引导程序是从文件尾往前搜自己的归档标记的，多出来的这段
    数据不影响它加载（已实测）。
    """
    build_id = uuid.uuid4().hex          # 32 位，换构建时触发重新解压
    size = payload.stat().st_size
    log("追加载荷到 exe（构建号 %s）…" % build_id[:8])

    with exe.open("ab") as out, payload.open("rb") as src:
        shutil.copyfileobj(src, out, 1 << 20)
        out.write(MAGIC + struct.pack("<Q", size) + build_id.encode("ascii"))


def verify(exe: Path) -> None:
    """用打包好的 exe 自己的逻辑读一遍尾部，确认结构没写错。"""
    sys.path.insert(0, str(ROOT))
    from dylive import runtime

    total = exe.stat().st_size
    with exe.open("rb") as fh:
        fh.seek(total - runtime.FOOTER_LEN)
        footer = fh.read(runtime.FOOTER_LEN)
    if not footer.startswith(MAGIC):
        raise SystemExit("尾部标记写入失败")
    size = struct.unpack("<Q", footer[16:24])[0]
    offset = total - runtime.FOOTER_LEN - size

    with runtime._Slice(exe, offset, size) as blob:
        with zipfile.ZipFile(blob) as zf:
            names = zf.namelist()
            bad = zf.testzip()
    if bad:
        raise SystemExit("载荷 zip 校验失败：%s" % bad)
    has_chrome = any(n.endswith("chrome.exe") for n in names)
    has_ffmpeg = "bin/ffmpeg.exe" in names
    has_ffprobe = "bin/ffprobe.exe" in names
    log("校验通过：%d 个条目，chrome.exe=%s ffmpeg.exe=%s ffprobe.exe=%s"
        % (len(names), has_chrome, has_ffmpeg, has_ffprobe))
    if not (has_chrome and has_ffmpeg and has_ffprobe):
        raise SystemExit("载荷内容不完整")


def smoke_test(exe: Path, full: bool = True) -> None:
    """按用户实际启动路径验证 Qt 界面；完整包再检查所有内置工具。"""
    with tempfile.TemporaryDirectory(prefix="lumina-build-smoke-") as tmp:
        env = os.environ.copy()
        env["LOCALAPPDATA"] = tmp
        env["APPDATA"] = str(Path(tmp) / "Roaming")
        checks = [("Qt 界面启动", [str(exe), "--smoke-test-gui"], 180)]
        if full:
            checks.append(
                ("完整运行环境", [str(exe), "--selftest-headless"], 240))
        for label, cmd, timeout in checks:
            log("成品自检：%s…" % label)
            try:
                proc = subprocess.run(cmd, env=env, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise SystemExit("成品自检超时（%s）" % label) from exc
            if proc.returncode != 0:
                crash = Path(tmp) / "Lumina" / "crash.log"
                detail = ""
                if crash.is_file():
                    detail = "\n" + crash.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise SystemExit("成品自检失败（%s），退出码 %d%s"
                                 % (label, proc.returncode, detail))
        log("成品自检全部通过")


def main() -> int:
    log("项目目录 %s" % ROOT)
    if "--qt-smoke-only" in sys.argv:
        log("CI 快速模式：只构建 PyInstaller 主程序并验证 Qt 界面启动")
        exe = run_pyinstaller(make_icon())
        smoke_test(exe, full=False)
        return 0

    chromium = find_chromium()
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    log("Chromium: %s" % chromium)
    log("ffmpeg  : %s（%s）" % (ffmpeg, human(ffmpeg.stat().st_size)))
    log("ffprobe : %s（%s）" % (ffprobe, human(ffprobe.stat().st_size)))

    icon = make_icon()
    log("图标：%s" % icon.name)

    exe = run_pyinstaller(icon)
    payload = build_payload(chromium, ffmpeg, ffprobe)
    append_payload(exe, payload)
    verify(exe)
    smoke_test(exe)

    log("=" * 56)
    log("完成：%s" % exe)
    log("大小：%s" % human(exe.stat().st_size))
    log("首次运行会解压内置运行时到 %%LOCALAPPDATA%%\\Lumina\\runtime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
