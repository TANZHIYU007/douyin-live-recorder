"""内置回放的语音和弹幕开关各自生效，换片不丢失用户选择。"""

import json
import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtMultimedia import QMediaMetaData
from PySide6.QtTest import QTest

from dylive.library import scan_library
from dylive.replay import Timeline, save_metadata
from dylive.ui import replay


def wait(qapp, predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def tracks(*titles):
    result = []
    for title in titles:
        track = QMediaMetaData()
        track.insert(QMediaMetaData.Title, title)
        result.append(track)
    return result


def test_native_chat_and_speech_toggles_preserve_data_and_user_choice(qapp, tmp_path,
                                                                    monkeypatch):
    prefix = tmp_path / "20261109_080000_弹幕"
    prefix.with_suffix(".jsonl").write_text(json.dumps({
        "kind": "chat", "offset": 1, "content": "消息", "user_name": "用户"}))
    dialog = replay.ReplayDialog(scan_library(tmp_path)[0])
    selected = []
    available = tracks("语音字幕", "弹幕字幕", "语音字幕 + 弹幕")
    monkeypatch.setattr(dialog.player, "subtitleTracks", lambda: available)
    monkeypatch.setattr(dialog.player, "setActiveSubtitleTrack", selected.append)
    try:
        wait(qapp, lambda: dialog._events_loaded)
        dialog._select_speech_subtitle()
        assert selected[-1] == 0  # JSONL 存在时，内嵌弹幕和兼容轨保持关闭。
        assert dialog.speech_subtitles.isEnabled()
        dialog.speech_subtitles.setChecked(False)
        assert selected[-1] == -1
        dialog.player.tracksChanged.emit()
        assert selected[-1] == -1
        dialog.show_danmaku.setChecked(False)
        assert dialog.chat.isHidden() and dialog.events_panel.isHidden()
        assert len(dialog.model.events) == 1
        dialog.show_danmaku.setChecked(True)
        assert not dialog.chat.isHidden() and not dialog.events_panel.isHidden()
        assert len(dialog.model.events) == 1
        available.clear()
        dialog.player.tracksChanged.emit()
        assert not dialog.speech_subtitles.isEnabled()
        assert not dialog.speech_subtitles.isChecked()
        available.extend(tracks("语音字幕", "弹幕字幕"))
        dialog.player.tracksChanged.emit()
        assert dialog.speech_subtitles.isEnabled()
        assert not dialog.speech_subtitles.isChecked()
        assert selected[-1] == -1
    finally:
        dialog.reject()


def test_without_jsonl_switches_combined_speech_chat_and_none(qapp, tmp_path, monkeypatch):
    path = tmp_path / "20261109_080000_仅内嵌.mkv"
    path.write_bytes(b"video")
    monkeypatch.setattr(replay, "build_timeline", lambda *_args, **_kwargs: Timeline())
    dialog = replay.ReplayDialog(scan_library(tmp_path)[0])
    selected = []
    available = tracks("弹幕字幕", "语音字幕", "语音字幕 + 弹幕")
    monkeypatch.setattr(dialog.player, "subtitleTracks", lambda: available)
    monkeypatch.setattr(dialog.player, "setActiveSubtitleTrack", selected.append)
    try:
        wait(qapp, lambda: dialog._events_loaded)
        dialog._select_speech_subtitle()
        assert selected[-1] == 2
        dialog.speech_subtitles.setChecked(False)
        assert selected[-1] == 0
        dialog.show_danmaku.setChecked(False)
        assert selected[-1] == -1
        dialog.speech_subtitles.setChecked(True)
        assert selected[-1] == 1
        dialog.show_danmaku.setChecked(True)
        assert selected[-1] == 2
    finally:
        dialog.reject()


def test_real_speech_toggle_remains_off_after_changing_video_segment(qapp, tmp_path):
    ffmpeg = os.environ.get("LUMINA_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("设置 LUMINA_TEST_FFMPEG 运行真实回放轨道切换测试")
    prefix = tmp_path / "20261109_080000_开关"
    srt = tmp_path / "track.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,500\n语音内容\n", encoding="utf-8")
    first = prefix.with_name(prefix.name + "_语音字幕.mkv")
    second = prefix.with_name(prefix.name + "_part001_语音字幕.mkv")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "color=c=black:size=160x90:rate=10", "-i", str(srt), "-i", str(srt),
        "-i", str(srt), "-map", "0:v", "-map", "1:s", "-map", "2:s", "-map", "3:s",
        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "srt",
        "-metadata:s:s:0", "title=语音字幕", "-metadata:s:s:1", "title=弹幕字幕",
        "-metadata:s:s:2", "title=语音字幕 + 弹幕", str(first)]
    result = subprocess.run(command, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    shutil.copyfile(first, second)
    prefix.with_suffix(".jsonl").write_text(json.dumps({
        "kind": "chat", "offset": 1, "content": "弹幕", "user_name": "用户"}))
    save_metadata(prefix, "123", [{"file": first.name, "start": 0, "duration": 3},
                                  {"file": second.name, "start": 3, "duration": 3}], {})
    session = next(s for s in scan_library(tmp_path) if s.prefix == prefix)
    dialog = replay.ReplayDialog(session, ffmpeg=ffmpeg)
    try:
        wait(qapp, lambda: len(dialog.timeline.segments) == 2)
        dialog._choose_segment(0)
        wait(qapp, lambda: len(dialog.player.subtitleTracks()) == 3, seconds=8)
        wait(qapp, lambda: dialog.player.activeSubtitleTrack() == 0)
        dialog.player.pause()
        assert dialog.speech_subtitles.isEnabled()
        dialog.speech_subtitles.setChecked(False)
        assert dialog.player.activeSubtitleTrack() == -1
        dialog._choose_segment(1)
        wait(qapp, lambda: len(dialog.player.subtitleTracks()) == 3
             and dialog.speech_subtitles.isEnabled(), seconds=8)
        dialog.player.pause()
        assert not dialog.speech_subtitles.isChecked()
        assert dialog.player.activeSubtitleTrack() == -1
        dialog.speech_subtitles.setChecked(True)
        assert dialog.player.activeSubtitleTrack() == 0
    finally:
        dialog.reject()
