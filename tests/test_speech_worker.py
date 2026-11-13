"""识别进程异常和取消不能污染主进程或改动录像。"""

import sys

import pytest

import gui
from dylive import speech_worker, transcription


def test_worker_missing_model_fails_cleanly_and_does_not_import_native_libraries(tmp_path,
                                                                                monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-models"))
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"source must stay intact")
    native_before = "av" in sys.modules
    with pytest.raises(transcription.TranscriptionError):
        speech_worker.run(source, model="tiny", output=tmp_path / "out.srt")
    assert ("av" in sys.modules) == native_before
    assert source.read_bytes() == b"source must stay intact"
    assert not (tmp_path / "out.srt").exists()


def test_worker_cancel_is_retryable_and_preserves_source(tmp_path):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"recording")
    with pytest.raises(transcription.TranscriptionCancelled):
        speech_worker.run(source, model="tiny", output=tmp_path / "out.srt", cancelled=lambda: True)
    assert source.read_bytes() == b"recording"
    assert not (tmp_path / "out.srt").exists()


def test_frozen_worker_routes_before_gui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Lumina.exe", "--speech-worker", "request", "result"])
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    calls = []
    monkeypatch.setattr(speech_worker, "main", lambda *args: calls.append(args) or 17)
    assert gui.main() == 17
    assert calls == [("request", "result")]
