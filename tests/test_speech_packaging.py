"""语音延迟依赖必须进入成品，并在成品检查中真正加载原生库。"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import build_app
import build_exe
import gui


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_packaging_preserves_speech_runtime_and_numpy(tmp_path, monkeypatch, platform):
    commands = []
    monkeypatch.setattr(build_exe, "DIST", tmp_path)
    monkeypatch.setattr(build_app, "DIST", tmp_path)
    monkeypatch.setattr(build_exe, "verify_pyinstaller_archive", lambda _: None)

    def run(command, **_kwargs):
        commands.append(command)
        (tmp_path / "Lumina-Lite.exe").write_bytes(b"package")
        (tmp_path / (build_app.APP_NAME + ".app")).mkdir(exist_ok=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_exe.subprocess, "run", run)
    if platform == "windows":
        build_exe.run_pyinstaller(tmp_path / "icon.ico", tmp_path / "version.txt", "Lumina-Lite")
    else:
        build_app.run_pyinstaller(tmp_path / "icon.icns", tmp_path / "runtime")
    command = commands[0]
    imports = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--hidden-import"]
    excludes = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--exclude-module"]
    data = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--collect-data"]
    binaries = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--collect-binaries"]
    assert {"faster_whisper", "av", "ctranslate2", "onnxruntime", "tokenizers", "numpy", "kaldi_native_fbank"} <= set(imports)
    assert "numpy" not in excludes
    assert "faster_whisper" in data  # Silero VAD lives in package data.
    assert {"ctranslate2", "onnxruntime"} <= set(binaries)
    assert not any("speech-models" in argument for argument in command)


@pytest.mark.parametrize("full", [False, True])
def test_both_windows_variants_run_offline_speech_check(tmp_path, monkeypatch, full):
    commands = []
    monkeypatch.setenv("LUMINA_GUI_SMOKE_MEDIA", str(tmp_path / "existing.mp4"))

    def run(command, **kwargs):
        commands.append(command)
        Path(kwargs["env"]["LUMINA_GUI_SMOKE_REPORT"]).write_text("gui passed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_exe.subprocess, "run", run)
    build_exe.smoke_test(tmp_path / "Lumina.exe", full=full)
    assert [str(tmp_path / "Lumina.exe"), "--selftest-speech-runtime"] in commands


@pytest.mark.parametrize("failure", [False, True])
def test_speech_runtime_check_loads_vad_without_model_or_gui(monkeypatch, failure):
    calls = []
    package, vad = ModuleType("faster_whisper"), ModuleType("faster_whisper.vad")
    fbank = ModuleType("kaldi_native_fbank")
    fbank.FbankOptions = lambda: None
    package.WhisperModel = lambda *_args, **_kwargs: pytest.fail("Must not load speech weights")

    def load_vad():
        calls.append("vad")
        if failure:
            raise RuntimeError("native library missing")

    vad.get_vad_model = load_vad
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setitem(sys.modules, "kaldi_native_fbank", fbank)
    monkeypatch.setattr(gui, "_on_uncaught", lambda *args, **kwargs: calls.append(kwargs))
    assert gui._run_speech_runtime_selftest() == (1 if failure else 0)
    assert calls[0] == "vad"
    if failure:
        assert calls[-1] == {"show_dialog": False}


def test_speech_runtime_command_routes_before_qt(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Lumina.exe", "--selftest-speech-runtime"])
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(gui, "_run_speech_runtime_selftest", lambda: 17)
    assert gui.main() == 17
