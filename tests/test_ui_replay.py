import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from dylive.library import scan_library
from dylive.processing import PostProcessQueue
from dylive.replay import ReplayEvent, Segment, Timeline, save_metadata
from dylive.ui.library import RecordingLibraryDialog
from dylive.ui.replay import EventModel, ReplayDialog
from dylive.ui.settings import AppSettings


def wait_for(qapp, predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def test_event_model_filters_plain_text(qapp):
    model = EventModel()
    model.set_events([ReplayEvent(1, "Alice", "<b>猫</b>", "chat"),
                      ReplayEvent(2, "Bob", "你好", "chat")])
    model.filter("ALICE")
    assert model.rowCount() == 1
    assert model.data(model.index(0, 3), Qt.DisplayRole) == "<b>猫</b>"
    model.filter("你好")
    assert model.visible[0].offset == 2


def make_session(folder, name="20261020_200000_测试"):
    folder.mkdir(exist_ok=True)
    path = folder / (name + ".jsonl")
    path.write_text('\n'.join(json.dumps({"offset": t, "user_name": "Alice",
                    "content": "你好" if t == 1 else "世界", "kind": "chat"})
                    for t in (1, 4)), encoding="utf-8")
    return scan_library(folder)[0]


def test_replay_no_video_supports_search_and_closes_worker(qapp, tmp_path):
    session = make_session(tmp_path)
    dialog = ReplayDialog(session)
    try:
        wait_for(qapp, lambda: len(dialog.model.events) == 2)
        assert not dialog.play_btn.isEnabled()
        assert "没有可用视频" in dialog.status.text()
        dialog.search.setText("世界")
        wait_for(qapp, lambda: dialog.model.rowCount() == 1)
        dialog.seek_event(4)
        assert "没有对应画面" in dialog.status.text()
        dialog.timeline = Timeline([Segment(Path("fake"), 2, 10)], delay=1)
        dialog.segment_index = 0
        dialog._position_changed(1000)  # event offset = 2
        assert "你好" in dialog.chat.text() and "世界" not in dialog.chat.text()
        dialog._position_changed(3000)  # event offset = 4
        assert "世界" in dialog.chat.text()
        dialog._position_changed(1500)  # 倒退不能保留未来弹幕。
        assert "世界" not in dialog.chat.text()
    finally:
        dialog.reject()
    assert dialog._cancel.is_set() and dialog.player.source().isEmpty()
    assert not dialog.filter_timer.isActive()


def test_library_searches_all_profile_roots_and_opens_matching_offset(qapp, tmp_path, monkeypatch):
    root, custom = tmp_path / "global", tmp_path / "custom"
    make_session(root)
    custom_session = make_session(custom, "20261020_210000_独立目录")
    queue = PostProcessQueue(tmp_path / "queue.json", autostart=False)
    settings = AppSettings(out_dir=str(root), room_profiles={"123": {"out_dir": str(custom)}})
    dialog = RecordingLibraryDialog(settings, queue)
    opened = []
    monkeypatch.setattr(dialog, "_show_replay", lambda session, offset=None: opened.append((session, offset)))
    try:
        wait_for(qapp, lambda: len(dialog.sessions) == 2)
        dialog.danmaku_query.setText("世界")
        dialog.danmaku_search_btn.click()
        wait_for(qapp, lambda: len(dialog._search_hits) == 2)
        assert dialog._search_hits[0][0].prefix == custom_session.prefix
        dialog._replay_hit(dialog.danmaku_results.item(0, 0))
        assert opened[0][0].prefix == custom_session.prefix and opened[0][1] == 4
        generation = dialog._search_generation
        dialog._cancel_search()
        dialog._on_searched(generation, [], "stale")
        assert dialog.danmaku_hint.text() == "已取消搜索"
    finally:
        dialog.reject()
        queue.shutdown()


def test_queue_uses_historical_recording_style_and_timeline(qapp, tmp_path):
    session = make_session(tmp_path)
    source = session.prefix.with_suffix(".mp4")
    source.touch()
    save_metadata(session.prefix, "123", [{"file": source.name, "start": 2.5, "duration": 10}],
                  {"subtitle_delay": 3, "subtitle_style": "scroll", "subtitle_size": 24})
    queue = PostProcessQueue(tmp_path / "queue.json", autostart=False)
    dialog = RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue)
    try:
        wait_for(qapp, lambda: len(dialog.sessions) == 1)
        dialog.queue_btn.click()
        task = queue.snapshot()[0]
        assert task.offsets == [2.5]
        assert task.style["delay"] == 3 and task.style["mode"] == "scroll"
        assert task.style["size"] == 24
    finally:
        dialog.reject()
        queue.shutdown()


def test_close_while_loading_ignores_late_result(qapp, tmp_path, monkeypatch):
    from dylive.ui import replay
    entered, release = threading.Event(), threading.Event()

    def load(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return [], type("Stats", (), {"skipped": 0, "truncated": False})()

    monkeypatch.setattr(replay, "load_events", load)
    dialog = ReplayDialog(make_session(tmp_path))
    assert entered.wait(2)
    dialog.reject()
    release.set()
    QTest.qWait(30)
    assert dialog.model.rowCount() == 0
