"""录后处理队列的串行执行、持久化和失败重试。"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dylive import processing, subtitle
from dylive.recorder import Options, Recorder


def _inputs(tmp_path):
    prefix = tmp_path / "20261009_203000_测试"
    jsonl = prefix.with_suffix(".jsonl")
    video = prefix.with_suffix(".mp4")
    jsonl.write_text('{"kind":"chat","content":"你好"}\n', encoding="utf-8")
    video.write_bytes(b"video")
    return prefix, jsonl, video


def test_queue_processes_and_persists_completed_task(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    state = tmp_path / "queue.json"
    calls = []

    def fake_processor(task, progress):
        calls.append(task.id)
        progress("正在封装测试文件…")
        result = Path(task.videos[0]).with_suffix(".mkv")
        result.write_bytes(b"done")
        return [result]

    queue = processing.PostProcessQueue(state, processor=fake_processor)
    try:
        task = queue.enqueue_subtitles(prefix, jsonl, [source])
        assert queue.wait_until_idle(3)
        finished = queue.snapshot()[0]
        assert finished.id == task.id
        assert finished.status == processing.COMPLETED
        assert finished.attempts == 1
        assert calls == [task.id]
        assert finished.results == [str(source.with_suffix(".mkv"))]
    finally:
        queue.shutdown()

    restored = processing.PostProcessQueue(state, processor=fake_processor,
                                           autostart=False)
    assert restored.snapshot()[0].status == processing.COMPLETED


def test_failed_task_can_be_retried(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    attempts = []

    def flaky(task, _progress):
        attempts.append(task.id)
        if len(attempts) == 1:
            raise RuntimeError("模拟失败")
        return []

    queue = processing.PostProcessQueue(tmp_path / "queue.json", processor=flaky)
    try:
        task = queue.enqueue_subtitles(prefix, jsonl, [source])
        assert queue.wait_until_idle(3)
        assert queue.snapshot()[0].status == processing.FAILED
        assert queue.retry(task.id)
        assert queue.wait_until_idle(3)
        retried = queue.snapshot()[0]
        assert retried.status == processing.COMPLETED
        assert retried.attempts == 2
        assert len(attempts) == 2
    finally:
        queue.shutdown()


def test_running_task_is_recovered_as_pending(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    state = tmp_path / "queue.json"
    task = processing.ProcessingTask(
        id="unfinished",
        session_prefix=str(prefix),
        jsonl=str(jsonl),
        videos=[str(source)],
        status=processing.RUNNING,
    )
    state.write_text(json.dumps([task.__dict__]), encoding="utf-8")

    queue = processing.PostProcessQueue(state, autostart=False)
    recovered = queue.snapshot()[0]
    assert recovered.status == processing.PENDING
    assert "等待继续" in recovered.message


def test_clear_finished_keeps_failed_tasks_for_retry(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    state = tmp_path / "queue.json"
    completed = processing.ProcessingTask(
        id="completed",
        session_prefix=str(prefix),
        jsonl=str(jsonl),
        videos=[str(source)],
        status=processing.COMPLETED,
    )
    failed = processing.ProcessingTask(
        id="failed",
        session_prefix=str(prefix),
        jsonl=str(jsonl),
        videos=[str(source)],
        status=processing.FAILED,
    )
    state.write_text(json.dumps([completed.__dict__, failed.__dict__]), encoding="utf-8")

    queue = processing.PostProcessQueue(state, autostart=False)
    assert queue.clear_finished() == 1
    assert [task.id for task in queue.snapshot()] == ["failed"]


def test_queue_stores_absolute_paths_and_matches_equivalent_prefix(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    prefix, jsonl, source = _inputs(work)
    monkeypatch.chdir(tmp_path)
    queue = processing.PostProcessQueue(tmp_path / "queue.json", autostart=False)

    task = queue.enqueue_subtitles(
        Path("work") / prefix.name,
        Path("work") / jsonl.name,
        [Path("work") / source.name],
    )

    assert Path(task.session_prefix).is_absolute()
    assert Path(task.jsonl).is_absolute()
    assert all(Path(path).is_absolute() for path in task.videos)
    assert queue.latest_for(prefix).id == task.id


def test_recorder_enqueues_automatic_subtitle_work(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    queued = []

    class FakeQueue:
        def enqueue_subtitles(self, *args, **kwargs):
            queued.append((args, kwargs))
            return SimpleNamespace(id="12345678abcdef")

    recorder = object.__new__(Recorder)
    recorder.opts = Options(target="123", embed_subtitle=True)
    recorder.post_queue = FakeQueue()
    recorder.ffmpeg = "unused"
    recorder._status_lock = threading.Lock()
    recorder._post = ""
    recorder._collect_parts = lambda: ([source], [1.25])

    recorder._embed_subtitles(prefix)

    assert len(queued) == 1
    args, kwargs = queued[0]
    assert args[:4] == (prefix, jsonl, [source], [1.25])
    assert kwargs["replace"] is True
    assert recorder._post == ""


@pytest.mark.parametrize("failure", ["mux", "validation"])
def test_subtitle_failure_is_retryable_and_keeps_original(tmp_path, monkeypatch, failure):
    prefix, jsonl, source = _inputs(tmp_path)
    monkeypatch.setattr(subtitle, "probe", lambda *_: {"duration": 10})

    def mux(_ffmpeg, _video, _ass, out):
        out.write_bytes(b"result")
        return failure != "mux"

    monkeypatch.setattr(subtitle, "mux", mux)
    monkeypatch.setattr(subtitle, "_looks_sane", lambda *_: False)
    queue = processing.PostProcessQueue(tmp_path / "queue.json",
                                        ffmpeg_provider=lambda: "unused")
    try:
        task = queue.enqueue_subtitles(prefix, jsonl, [source])
        assert queue.wait_until_idle(3)
        failed = queue.snapshot()[0]
        assert failed.status == processing.FAILED
        assert source.name in failed.error
        assert source.read_bytes() == b"video"
        queue.shutdown()
        assert queue.retry(task.id)
    finally:
        queue.shutdown()


def test_partial_failure_retries_only_unfinished_segments_after_restart(tmp_path, monkeypatch):
    prefix, jsonl, first = _inputs(tmp_path)
    second = prefix.with_name(prefix.name + "_part001.mp4")
    second.write_bytes(b"second")
    jsonl.write_text("\n".join(json.dumps({"kind": "chat", "content": "test", "offset": t})
                               for t in (1, 11)), encoding="utf-8")
    monkeypatch.setattr(subtitle, "probe", lambda *_: {"duration": 10})
    monkeypatch.setattr(subtitle, "_looks_sane", lambda *_: True)
    calls = []

    def mux(_ffmpeg, video, ass, out):
        calls.append(video)
        if video == second and calls.count(second) == 1:
            return False
        assert "0:00:01.00" in ass.read_text(encoding="utf-8-sig")
        out.write_bytes(b"finished")
        return True

    monkeypatch.setattr(subtitle, "mux", mux)
    state = tmp_path / "queue.json"
    queue = processing.PostProcessQueue(state, ffmpeg_provider=lambda: "unused")
    try:
        task = queue.enqueue_subtitles(prefix, jsonl, [first, second])
        assert queue.wait_until_idle(3)
        failed = queue.snapshot()[0]
        assert failed.status == processing.FAILED
        assert failed.results == [str(first.with_suffix(".mkv"))]
        assert not first.exists()
        assert second.exists()
    finally:
        queue.shutdown()

    restored = processing.PostProcessQueue(state, ffmpeg_provider=lambda: "unused")
    try:
        assert restored.retry(task.id)
        assert restored.wait_until_idle(3)
        done = restored.snapshot()[0]
        assert done.status == processing.COMPLETED
        assert done.results == [str(first.with_suffix(".mkv")), str(second.with_suffix(".mkv"))]
        assert calls == [first, second, second]
        assert not second.exists()
    finally:
        restored.shutdown()


def test_empty_danmaku_is_successful_noop(tmp_path):
    prefix, jsonl, source = _inputs(tmp_path)
    jsonl.write_text("", encoding="utf-8")
    queue = processing.PostProcessQueue(tmp_path / "queue.json",
                                        ffmpeg_provider=lambda: "unused")
    try:
        queue.enqueue_subtitles(prefix, jsonl, [source])
        assert queue.wait_until_idle(3)
        done = queue.snapshot()[0]
        assert done.status == processing.COMPLETED
        assert done.results == []
        assert source.read_bytes() == b"video"
    finally:
        queue.shutdown()
