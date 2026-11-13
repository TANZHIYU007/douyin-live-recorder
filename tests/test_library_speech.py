"""语音字幕成品归入原场次，时间轴和手动入口保持一致。"""

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from dylive.library import recording_prefix, scan_library
from dylive.replay import current_videos, part_key, save_metadata, saved_offsets


def test_speech_outputs_and_sidecars_stay_in_one_session(tmp_path):
    prefix = tmp_path / "20261109_080000_带.句点标题"
    names = [prefix.name + tail for tail in (
        ".mp4", "_r1_part002.mp4", "_语音字幕.mkv", "_语音字幕_2.mkv",
        "_r1_part002_弹幕版_语音字幕_3.mkv", "_语音字幕_弹幕.ass",
        "_r1_part002_语音字幕_2_弹幕.ass", "_语音识别-abcd1234.srt",
        "_r1_part002_弹幕版_语音识别-abcd1234.srt", ".jsonl")]
    for name in names:
        (tmp_path / name).write_bytes(b"data")
    temporary = tmp_path / ".lumina-speech-job"
    temporary.mkdir()
    (temporary / "output.mkv").write_bytes(b"unfinished")

    sessions = scan_library(tmp_path)

    assert len(sessions) == 1
    assert sessions[0].prefix == prefix
    assert len(sessions[0].files) == len(names)
    for name in names:
        assert recording_prefix(tmp_path / name) == prefix


def test_speech_copies_keep_part_offsets_and_do_not_duplicate_playback(tmp_path):
    prefix = tmp_path / "20261109_080000_场次"
    first = prefix.with_name(prefix.name + ".mp4")
    second = prefix.with_name(prefix.name + "_part001.mp4")
    speech_first = prefix.with_name(prefix.name + "_语音字幕.mkv")
    speech_newest = prefix.with_name(prefix.name + "_语音字幕_4.mkv")
    speech_second = prefix.with_name(prefix.name + "_part001_语音字幕_2.mkv")
    for path in (first, second, speech_first, speech_newest, speech_second):
        path.write_bytes(b"video")
    save_metadata(prefix, "123", [{"file": first.name, "start": 3, "duration": 10},
                                  {"file": second.name, "start": 20, "duration": 8}], {})
    session = scan_library(tmp_path)[0]

    assert current_videos(session) == [speech_newest, speech_second]
    assert current_videos(session, prefer_speech=False) == [first, second]
    assert part_key(speech_second) == second.stem
    assert saved_offsets(prefix, current_videos(session)) == [3, 20]


def _wait(qapp, predicate, seconds=4):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def test_manual_speech_action_handles_session_without_danmaku(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from dylive.processing import PostProcessQueue
    from dylive import transcription as backend
    from dylive.ui import library, transcription
    from dylive.ui.settings import AppSettings
    prefix = tmp_path / "20261109_080000_仅视频"
    source = prefix.with_name(prefix.name + ".mp4")
    source.write_bytes(b"video")
    save_metadata(prefix, "123", [{"file": source.name, "start": 5, "duration": 10}],
                  {"subtitle_style": "scroll", "subtitle_size": 24})
    queue = PostProcessQueue(tmp_path / "queue.json", autostart=False)
    accepted = []

    class Selection:
        def __init__(self, settings, parent):
            accepted.append(parent)

        def exec(self):
            return QDialog.Accepted

        def selected_options(self):
            return {"model": "tiny", "language": "zh", "subtitle_mode": "combined"}

    monkeypatch.setattr(transcription, "TranscriptionDialog", Selection)
    monkeypatch.setattr(backend, "dependency_available", lambda: True)
    monkeypatch.setattr(backend, "model_status", lambda model: True)
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    dialog = library.RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue)
    try:
        _wait(qapp, lambda: len(dialog.sessions) == 1)
        assert dialog.transcribe_btn.isEnabled()
        assert not dialog.queue_btn.isEnabled()
        dialog.transcribe_btn.click()
        task = queue.snapshot()[0]
        assert accepted == [dialog]
        assert task.task_type == "transcription" and task.videos == [str(source)]
        assert task.jsonl == "" and task.offsets == [5]
        assert task.speech["model"] == "tiny" and task.speech["language"] == "zh"
        assert task.style["mode"] == "scroll" and task.style["size"] == 24
        assert not dialog.transcribe_btn.isEnabled()
        assert "语音转文字" in dialog._task_values(task)[1]
        dialog.cancel_task_btn.click()
        assert queue.snapshot()[0].status == "cancelled"
        assert dialog.transcribe_btn.isEnabled()
    finally:
        dialog.reject()
        queue.shutdown()


def test_speech_action_disabled_for_currently_recording_session(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget
    from dylive.processing import PostProcessQueue
    from dylive.ui.library import RecordingLibraryDialog
    from dylive.ui.settings import AppSettings
    source = tmp_path / "20261109_080000_正在录制.mp4"
    source.write_bytes(b"video")
    parent = QWidget()
    recorder = SimpleNamespace(_prefix=source.with_suffix(""),
                               status=lambda: SimpleNamespace(running=True))
    parent.manager = SimpleNamespace(rooms=lambda: [SimpleNamespace(_recorder=recorder)])
    queue = PostProcessQueue(tmp_path / "queue.json", autostart=False)
    dialog = RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue, parent)
    try:
        _wait(qapp, lambda: len(dialog.sessions) == 1)
        assert not dialog.transcribe_btn.isEnabled()
        assert "仍在录制" in dialog.transcribe_btn.toolTip()
    finally:
        dialog.reject()
        queue.shutdown()
        parent.close()


def test_replay_selects_voice_track_and_keeps_embedded_danmaku_disabled(qapp, tmp_path,
                                                                    monkeypatch):
    from PySide6.QtMultimedia import QMediaMetaData
    from dylive.ui.replay import ReplayDialog
    path = tmp_path / "20261109_080000_字幕.jsonl"
    path.write_text(json.dumps({"kind": "chat", "offset": 1,
                               "content": "弹幕", "user_name": "测试"}))
    session = scan_library(tmp_path)[0]
    dialog = ReplayDialog(session)
    tracks = []
    for title in ("弹幕", "语音字幕", "语音字幕 + 弹幕"):
        meta = QMediaMetaData()
        meta.insert(QMediaMetaData.Title, title)
        tracks.append(meta)
    selected = []
    monkeypatch.setattr(dialog.player, "subtitleTracks", lambda: tracks)
    monkeypatch.setattr(dialog.player, "setActiveSubtitleTrack", selected.append)
    try:
        dialog._select_speech_subtitle()
        assert selected[-1] == 1
        tracks.pop(1)
        dialog._select_speech_subtitle()
        assert selected[-1] == -1
    finally:
        dialog.reject()


def test_real_replay_enables_muxed_speech_track(qapp, tmp_path):
    ffmpeg = os.environ.get("LUMINA_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("设置 LUMINA_TEST_FFMPEG 运行真实字幕轨回放")
    from PySide6.QtMultimedia import QMediaMetaData
    from dylive.ui.replay import ReplayDialog
    prefix = tmp_path / "20261109_080000_真实字幕"
    srt = prefix.with_name(prefix.name + "_语音识别-abcd1234.srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n语音测试\n", encoding="utf-8")
    video = prefix.with_name(prefix.name + "_语音字幕.mkv")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:size=160x90:rate=10", "-i", str(srt),
        "-i", str(srt), "-map", "0:v", "-map", "1:s", "-map", "2:s", "-t", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "srt",
        "-metadata:s:s:0", "title=弹幕", "-metadata:s:s:1", "title=语音字幕", str(video)],
        capture_output=True, timeout=15)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    session = scan_library(tmp_path)[0]
    dialog = ReplayDialog(session, ffmpeg=ffmpeg)
    try:
        _wait(qapp, lambda: len(dialog.timeline.segments) == 1)
        dialog._choose_segment(0)
        _wait(qapp, lambda: len(dialog.player.subtitleTracks()) == 2, seconds=8)
        _wait(qapp, lambda: dialog.player.activeSubtitleTrack() >= 0)
        active = dialog.player.subtitleTracks()[dialog.player.activeSubtitleTrack()]
        assert active.stringValue(QMediaMetaData.Title) == "语音字幕"
    finally:
        dialog.reject()
