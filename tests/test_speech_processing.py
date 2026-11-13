"""语音处理任务的部分完成、重启恢复、取消和源文件保护。"""

import json
import threading
from types import SimpleNamespace

import pytest

from dylive import processing, transcription


@pytest.fixture
def speech_inputs(tmp_path, monkeypatch):
    prefix = tmp_path / "20261109_080000_测试.标题"
    first = prefix.with_name(prefix.name + ".mp4")
    second = prefix.with_name(prefix.name + "_part001.mp4")
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(transcription, "dependency_available", lambda: True)
    monkeypatch.setattr(transcription, "model_status", lambda _: True)
    return prefix, [first, second]


def test_transcription_partial_failure_restarts_from_saved_srt(speech_inputs, tmp_path,
                                                              monkeypatch):
    from dylive import speech_subtitle
    prefix, sources = speech_inputs
    transcribed, muxed = [], []

    def transcribe(source, **kwargs):
        transcribed.append(source)
        kwargs["output"].write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
        return SimpleNamespace(segments=[object()])

    def mux(_ffmpeg, source, _srt, **kwargs):
        muxed.append(source)
        if source == sources[1] and muxed.count(source) == 1:
            raise RuntimeError("模拟第二段封装失败")
        kwargs["out"].write_bytes(b"muxed")
        return kwargs["out"]

    monkeypatch.setattr(transcription, "transcribe_isolated", transcribe)
    monkeypatch.setattr(speech_subtitle, "mux_speech", mux)
    state = tmp_path / "queue.json"
    queue = processing.PostProcessQueue(state, ffmpeg_provider=lambda: "unused")
    try:
        task = queue.enqueue_transcription(prefix, sources, [3, 13])
        assert queue.wait_until_idle(3)
        failed = queue.snapshot()[0]
        assert failed.status == processing.FAILED
        assert failed.processed_videos == [str(sources[0])]
        assert len(failed.speech["srt_ready"]) == 2
        assert all(source.exists() for source in sources)
    finally:
        queue.shutdown()
    resumed = processing.PostProcessQueue(state, ffmpeg_provider=lambda: "unused")
    try:
        assert resumed.retry(task.id)
        assert resumed.wait_until_idle(3)
        done = resumed.snapshot()[0]
        assert done.status == processing.COMPLETED
        assert transcribed == sources
        assert muxed == [sources[0], sources[1], sources[1]]
        assert len(done.results) == 4  # 两份 SRT 和两段视频。
        assert done.offsets == [3, 13]
    finally:
        resumed.shutdown()


def test_cancel_pending_speech_preserves_input_and_can_retry(speech_inputs, tmp_path):
    prefix, sources = speech_inputs
    queue = processing.PostProcessQueue(tmp_path / "q.json", autostart=False)
    task = queue.enqueue_transcription(prefix, sources, [0, 10])
    assert queue.cancel(task.id)
    assert queue.snapshot()[0].status == processing.CANCELLED
    assert queue.wait_until_idle(.1)
    assert queue.retry(task.id)
    assert queue.snapshot()[0].status == processing.PENDING
    assert sources[0].read_bytes() == b"first"


def test_cancel_running_speech_remains_retryable(speech_inputs, tmp_path, monkeypatch):
    prefix, sources = speech_inputs
    entered = threading.Event()

    def transcribe(_source, **kwargs):
        entered.set()
        while not kwargs["cancelled"]():
            threading.Event().wait(.01)
        raise transcription.TranscriptionCancelled("已取消")

    monkeypatch.setattr(transcription, "transcribe_isolated", transcribe)
    queue = processing.PostProcessQueue(tmp_path / "q.json", ffmpeg_provider=lambda: "unused")
    try:
        task = queue.enqueue_transcription(prefix, sources, [0, 10])
        assert entered.wait(2)
        assert queue.cancel(task.id)
        assert queue.wait_until_idle(3)
        assert queue.snapshot()[0].status == processing.CANCELLED
        assert all(source.exists() for source in sources)
    finally:
        queue.shutdown()


def test_silence_is_completed_without_mux_or_empty_sidecar(speech_inputs, tmp_path,
                                                         monkeypatch):
    from dylive import speech_subtitle
    prefix, sources = speech_inputs
    monkeypatch.setattr(transcription, "transcribe_isolated",
                        lambda *args, **kwargs: SimpleNamespace(segments=[]))
    monkeypatch.setattr(speech_subtitle, "mux_speech",
                        lambda *args, **kwargs: pytest.fail("静音不应封装空字幕"))
    queue = processing.PostProcessQueue(tmp_path / "q.json", ffmpeg_provider=lambda: "unused")
    try:
        queue.enqueue_transcription(prefix, sources, [0, 10])
        assert queue.wait_until_idle(3)
        done = queue.snapshot()[0]
        assert done.status == processing.COMPLETED and done.results == []
        assert "未识别到语音" in done.message
        assert not list(tmp_path.glob("*.srt"))
    finally:
        queue.shutdown()


def test_task_blocks_duplicate_session_and_detects_source_change(speech_inputs, tmp_path):
    prefix, sources = speech_inputs
    queue = processing.PostProcessQueue(tmp_path / "q.json", autostart=False,
                                        ffmpeg_provider=lambda: "unused")
    queue.enqueue_transcription(prefix, sources, [0, 10])
    with pytest.raises(ValueError, match="已有"):
        queue.enqueue_transcription(prefix, sources, [0, 10])
    sources[0].write_bytes(b"replaced")
    queue.start()
    try:
        assert queue.wait_until_idle(3)
        assert queue.snapshot()[0].status == processing.FAILED
        assert "已变化" in queue.snapshot()[0].error
    finally:
        queue.shutdown()


def test_speech_output_does_not_replace_existing_user_video(speech_inputs, tmp_path):
    prefix, sources = speech_inputs
    existing = prefix.with_name(prefix.name + "_语音字幕.mkv")
    existing.write_bytes(b"user file")
    queue = processing.PostProcessQueue(tmp_path / "q.json", autostart=False)
    task = queue.enqueue_transcription(prefix, sources, [0, 10])
    assert task.speech["output_files"][str(sources[0])].endswith("_语音字幕_2.mkv")
    assert existing.read_bytes() == b"user file"
    restored = json.loads((tmp_path / "q.json").read_text())
    assert restored[0]["task_type"] == "transcription" and restored[0]["replace"] is False


def test_crash_window_preserves_uncheckpointed_outputs(speech_inputs, tmp_path, monkeypatch):
    from dylive import speech_subtitle
    prefix, sources = speech_inputs
    queue = processing.PostProcessQueue(tmp_path / "q.json", autostart=False,
                                        ffmpeg_provider=lambda: "unused")
    task = queue.enqueue_transcription(prefix, sources[:1], [0])
    from pathlib import Path
    orphan_srt = Path(task.speech["srt_files"][str(sources[0])])
    orphan_video = Path(task.speech["output_files"][str(sources[0])])
    orphan_srt.write_text("keep this subtitle")
    orphan_video.write_bytes(b"keep this video")

    def transcribe(_source, **kwargs):
        assert kwargs["output"] != orphan_srt
        kwargs["output"].write_text("new srt")
        return SimpleNamespace(segments=[object()], language="zh")

    def mux(_ffmpeg, _source, _srt, **kwargs):
        assert kwargs["out"] != orphan_video
        assert kwargs["language"] == "zh"
        kwargs["out"].write_bytes(b"new output")
        return kwargs["out"]

    monkeypatch.setattr(transcription, "transcribe_isolated", transcribe)
    monkeypatch.setattr(speech_subtitle, "mux_speech", mux)
    queue.start()
    try:
        assert queue.wait_until_idle(3)
        assert queue.snapshot()[0].status == processing.COMPLETED
        assert orphan_srt.read_text() == "keep this subtitle"
        assert orphan_video.read_bytes() == b"keep this video"
    finally:
        queue.shutdown()


@pytest.mark.parametrize("offsets", [[float("nan"), 0], [0], [-1, 2]])
def test_invalid_part_offsets_rejected(speech_inputs, tmp_path, offsets):
    prefix, sources = speech_inputs
    queue = processing.PostProcessQueue(tmp_path / "q.json", autostart=False)
    with pytest.raises(ValueError, match="时间记录"):
        queue.enqueue_transcription(prefix, sources, offsets)
