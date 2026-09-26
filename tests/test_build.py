"""发布包必须同时包含录制和媒体探测工具。"""

import zipfile

import build_exe


def test_find_ffprobe_优先找_ffmpeg_同目录(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    assert build_exe.find_ffprobe(ffmpeg) == ffprobe


def test_find_chromium_使用_playwright_要求的精确版本(tmp_path, monkeypatch):
    (tmp_path / "chromium-1234").mkdir()
    expected = tmp_path / "chromium-1243"
    expected.mkdir()
    monkeypatch.setattr(build_exe, "playwright_root", lambda: tmp_path)
    monkeypatch.setattr(build_exe, "expected_chromium_revision", lambda: "1243")

    assert build_exe.find_chromium() == expected


def test_windows_载荷包含_ffmpeg_和_ffprobe(tmp_path, monkeypatch):
    chromium = tmp_path / "chromium-1"
    chromium.mkdir()
    (chromium / "chrome.exe").write_bytes(b"chrome")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(build_exe, "BUILD", build)

    payload = build_exe.build_payload(chromium, ffmpeg, ffprobe)
    with zipfile.ZipFile(payload) as zf:
        names = set(zf.namelist())

    assert "bin/ffmpeg.exe" in names
    assert "bin/ffprobe.exe" in names
    assert any(name.endswith("chrome.exe") for name in names)


def test_打包_path_排除第三方_icu_但保留系统目录(tmp_path):
    windows = tmp_path / "Windows"
    system32 = windows / "System32"
    poppler = tmp_path / "poppler" / "bin"
    normal = tmp_path / "normal"
    for folder in (system32, poppler, normal):
        folder.mkdir(parents=True)
    (system32 / "icuuc.dll").write_bytes(b"system")
    (poppler / "icuuc.dll").write_bytes(b"third-party")

    value = build_exe.os.pathsep.join(map(str, (poppler, system32, normal)))
    cleaned, removed = build_exe.sanitized_build_path(value, windows)

    assert str(poppler) not in cleaned.split(build_exe.os.pathsep)
    assert str(system32) in cleaned.split(build_exe.os.pathsep)
    assert str(normal) in cleaned.split(build_exe.os.pathsep)
    assert removed == [str(poppler)]


def test_归档检查识别根目录_icu_污染():
    names = [
        "PySide6\\QtCore.pyd",
        "PySide6\\Qt6Core.dll",
        "icuuc.dll",
        "icudt78.dll",
        "somewhere\\icuuc.dll",
    ]

    assert build_exe.unexpected_root_icu(names) == ["icudt78.dll", "icuuc.dll"]
