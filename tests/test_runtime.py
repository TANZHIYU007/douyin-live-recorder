"""打包版首次解压与损坏恢复。"""

import io
import sys
import threading
import time
import zipfile

from dylive import runtime


def _fake_frozen_exe(tmp_path, monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ms-playwright/chromium-123/chrome-win64/chrome.exe", b"chrome")
        zf.writestr(runtime.FFMPEG_REL, b"ffmpeg")
        zf.writestr(runtime.FFPROBE_REL, b"ffprobe")
        for index in range(40):
            zf.writestr("data/%03d.txt" % index, b"x" * 1024)

    build_id = "a" * runtime.BUILD_ID_LEN
    raw = payload.getvalue()
    exe = tmp_path / "Lumina.exe"
    exe.write_bytes(b"fake-exe" + raw + runtime.MAGIC
                    + len(raw).to_bytes(8, "little") + build_id.encode("ascii"))
    app_dir = tmp_path / "appdata" / "Lumina"
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.paths, "app_data_dir", lambda: app_dir)
    return app_dir


def test_解压完整后再次调用直接复用(tmp_path, monkeypatch):
    app_dir = _fake_frozen_exe(tmp_path, monkeypatch)

    runtime.extract()
    marker = app_dir / "runtime" / runtime._STAMP
    first_mtime = marker.stat().st_mtime_ns
    runtime.extract()

    assert runtime.is_ready()
    assert marker.stat().st_mtime_ns == first_mtime
    assert not list(app_dir.glob(".runtime-preparing-*"))


def test_运行时文件损坏会自动重新解压(tmp_path, monkeypatch):
    app_dir = _fake_frozen_exe(tmp_path, monkeypatch)
    runtime.extract()
    ffmpeg = app_dir / "runtime" / runtime.FFMPEG_REL
    ffmpeg.unlink()

    assert not runtime.is_ready()
    runtime.extract()
    assert ffmpeg.read_bytes() == b"ffmpeg"
    assert runtime.is_ready()


def test_两个进程式调用不会互相删除文件(tmp_path, monkeypatch):
    app_dir = _fake_frozen_exe(tmp_path, monkeypatch)
    original = zipfile.ZipFile.extract
    entered = threading.Event()

    def slow_extract(self, member, path=None, pwd=None):
        entered.set()
        time.sleep(0.003)
        return original(self, member, path, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "extract", slow_extract)
    errors = []

    def work():
        try:
            runtime.extract()
        except Exception as exc:  # noqa: BLE001 - 两条线程的异常要带回测试线程
            errors.append(exc)

    first = threading.Thread(target=work)
    second = threading.Thread(target=work)
    first.start()
    assert entered.wait(2)
    second.start()
    first.join(5)
    second.join(5)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert runtime.is_ready()
    assert not list(app_dir.glob(".runtime-preparing-*"))


def test_轻量版优先使用系统_edge(tmp_path, monkeypatch):
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"edge")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime, "read_footer", lambda: None)
    monkeypatch.setattr(runtime, "bundled_runtime", lambda: None)
    monkeypatch.setattr(runtime.paths, "WINDOWS", True)
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert runtime.browser_channel() == "msedge"
    assert "轻量版" in runtime.describe()
