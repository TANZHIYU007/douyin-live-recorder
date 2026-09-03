#!/usr/bin/env python
"""把「拾光 Lumina」打成 macOS 的 .app。

    python3 build_app.py

**必须在 Mac 上跑。** PyInstaller 不能交叉编译 —— 它是把当前解释器和当前
平台的动态库收集起来打包，在 Windows 上跑只能得到 exe。

和 Windows 版（build_exe.py）的两点区别：

1. 不用「追加到可执行文件尾部」那套。.app 本身就是个目录，Chromium 和
   ffmpeg 直接放进 Contents/Resources/runtime 就行，首次启动连解压都不用。
   而且在 macOS 上给可执行文件屁股后面加字节会**破坏代码签名**，Apple 芯片
   上签名一坏就是启动即闪退（Killed: 9）。

2. 因此用 --onedir 而不是 --onefile：.app 反正是目录，onefile 只会白白
   多一次每次启动的解压。

产物：dist/拾光.app
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "拾光"
BUNDLE_ID = "com.lumina.shiguang"

sys.path.insert(0, str(ROOT))
from build_exe import (OTHER_EXCLUDES, QT_EXCLUDES, find_chromium,  # noqa: E402
                       human, log)


def require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "这个脚本只能在 macOS 上运行。PyInstaller 不支持交叉编译：\n"
            "  * 在 Windows 上打包 → build_exe.py，得到 Lumina.exe\n"
            "  * 在 Mac 上打包     → 本脚本，得到 拾光.app\n"
            "两边的源码是同一份，不用改任何东西。")


def find_ffmpeg() -> Path:
    """macOS 上的 ffmpeg。要和目标机器同架构（Apple 芯片就得是 arm64）。"""
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit(
            "PATH 里找不到 ffmpeg。装一个再来：brew install ffmpeg\n"
            "（Apple 芯片的机器请确认装的是 arm64 版，用 file $(which ffmpeg) 看一眼）")
    path = Path(found).resolve()
    arch = subprocess.run(["file", str(path)], capture_output=True, text=True)
    log("ffmpeg：%s（%s）" % (path, arch.stdout.strip().split(":")[-1].strip()))
    return path


def make_icns() -> Path:
    """用 Qt 画出图标再交给 iconutil 转成 icns。"""
    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import (QColor, QFont, QGuiApplication, QLinearGradient,
                               QPainter, QPixmap)

    app = QGuiApplication.instance() or QGuiApplication([])   # noqa: F841
    iconset = BUILD / "lumina.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    # icns 要的是这一整套尺寸，少一个 iconutil 就不干
    for size in (16, 32, 64, 128, 256, 512, 1024):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#ff2d55"))
        grad.setColorAt(1.0, QColor("#7c4dff"))
        painter.setBrush(grad)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
        font = QFont("PingFang SC", int(size * 0.46))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(pix.rect(), Qt.AlignCenter, "拾")
        painter.end()

        pix.save(str(iconset / ("icon_%dx%d.png" % (size, size))))
        if size <= 512:                     # @2x 是给 Retina 用的
            pix.save(str(iconset / ("icon_%dx%d@2x.png" % (size // 2, size // 2))))

    icns = BUILD / "lumina.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True)
    log("图标：%s" % icns)
    return icns


def stage_runtime(chromium: Path, ffmpeg: Path) -> Path:
    """把浏览器和 ffmpeg 摆成 runtime/ 目录，整个塞进 .app 的 Resources。"""
    stage = BUILD / "runtime"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "bin").mkdir(parents=True)

    target = stage / "ms-playwright" / chromium.name
    log("拷贝 Chromium…（几百 MB，要一会儿）")
    shutil.copytree(chromium, target, symlinks=True)
    shutil.copy2(ffmpeg, stage / "bin" / "ffmpeg")
    os.chmod(stage / "bin" / "ffmpeg", 0o755)

    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    log("运行时共 %s" % human(size))
    return stage


def run_pyinstaller(icns: Path, runtime: Path) -> Path:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--windowed", "--name", APP_NAME,
           "--icon", str(icns),
           "--osx-bundle-identifier", BUNDLE_ID,
           # 冒号是 macOS/Linux 的分隔符（Windows 用分号）
           "--add-data", "%s:runtime" % runtime,
           "--collect-data", "playwright",
           "--collect-binaries", "playwright",
           "--hidden-import", "dylive.ui.window"]
    for mod in QT_EXCLUDES + OTHER_EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(ROOT / "gui.py"))

    log("运行 PyInstaller…（十几分钟，Chromium 那几百个文件要一个个过）")
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit("PyInstaller 失败，退出码 %d" % proc.returncode)

    app = DIST / (APP_NAME + ".app")
    if not app.is_dir():
        raise SystemExit("没找到产物 %s" % app)
    return app


def patch_plist(app: Path) -> None:
    """补几条 Info.plist：中文名、高分屏、以及别在 Dock 上显示成 python。"""
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as fh:
        info = plistlib.load(fh)
    info.update({
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": "拾光 · 抖音直播录制",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # 录制要一直跑，别让系统把它当成闲置应用压下去
        "LSUIElement": False,
        "NSAppleEventsUsageDescription": "用于在访达中打开录制目录",
    })
    with plist_path.open("wb") as fh:
        plistlib.dump(info, fh)
    log("Info.plist 已更新")


def sign(app: Path) -> None:
    """做一次 ad-hoc 签名。

    Apple 芯片上，**没有任何签名的可执行文件根本跑不起来**（直接 Killed: 9）。
    ad-hoc 签名（-s -）足够让它在本机跑；要发给别人还得有开发者证书并做公证，
    否则对方要右键「打开」，或者执行：
        xattr -dr com.apple.quarantine /Applications/拾光.app
    """
    log("ad-hoc 签名…")
    proc = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        log("签名失败（Intel 机器上可以忽略，Apple 芯片上必须解决）：%s"
            % proc.stderr.strip()[:300])
        return
    check = subprocess.run(["codesign", "--verify", "--verbose", str(app)],
                           capture_output=True, text=True)
    log("签名校验：%s" % (check.stderr.strip() or "通过"))


def verify(app: Path) -> None:
    exe = app / "Contents" / "MacOS" / APP_NAME
    runtime = app / "Contents" / "Frameworks" / "runtime"
    if not runtime.is_dir():                # PyInstaller 版本不同，位置会变
        runtime = app / "Contents" / "Resources" / "runtime"
    problems = []
    if not exe.is_file():
        problems.append("找不到主程序 %s" % exe)
    if not runtime.is_dir():
        problems.append("运行时没进包，Contents 下没有 runtime/")
    else:
        if not (runtime / "bin" / "ffmpeg").is_file():
            problems.append("runtime/bin/ffmpeg 不在")
        if not list((runtime / "ms-playwright").glob("chromium*")):
            problems.append("runtime/ms-playwright 下没有 chromium")
    if problems:
        raise SystemExit("产物有问题：\n  " + "\n  ".join(problems))

    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file())
    log("校验通过，%s 共 %s" % (app.name, human(size)))


def main() -> int:
    require_macos()
    BUILD.mkdir(exist_ok=True)
    chromium = find_chromium()
    ffmpeg = find_ffmpeg()
    icns = make_icns()
    runtime = stage_runtime(chromium, ffmpeg)
    app = run_pyinstaller(icns, runtime)
    patch_plist(app)
    sign(app)
    verify(app)
    log("完成：%s" % app)
    log("先自检一遍：%s/Contents/MacOS/%s --selftest" % (app, APP_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
