import json
import threading
from types import SimpleNamespace

import pytest

from dylive import replay
from dylive.library import scan_library
from dylive.recorder import Options, Recorder


def session_fixture(tmp_path, names=("", "_r2", "_r10")):
    folder = tmp_path / "主播"
    folder.mkdir(exist_ok=True)
    prefix = folder / "20261020_200000_直播"
    for name in names:
        prefix.with_name(prefix.name + name + ".mp4").touch()
    prefix.with_suffix(".jsonl").write_text("\n".join(json.dumps({
        "offset": t, "user_name": user, "content": content, "kind": "chat"}, ensure_ascii=False)
        for t, user, content in [(3.5, "Alice", "你好"), (1, "路人", "猫猫"),
                                  (15, "Bob", "Alice来了")]), encoding="utf-8")
    return scan_library(tmp_path)[0]


def test_load_sorts_and_searches_user_content_case_insensitively(tmp_path):
    session = session_fixture(tmp_path)
    events, stats = replay.load_events(session.jsonl)
    assert [e.offset for e in events] == [1, 3.5, 15]
    assert stats.skipped == 0
    hits, note = replay.search_sessions([session], "alice", threading.Event())
    assert [event.offset for _, event in hits] == [3.5, 15]
    assert "2 条" in note


@pytest.mark.parametrize("bad", ["null", "[]", "{", '{"offset": true}',
                                  '{"offset": -1}', '{"offset": NaN}',
                                  '{"offset": 1e99}', '{"offset": 2, "user_name": []}'])
def test_corrupt_records_skipped_without_losing_good_records(tmp_path, bad):
    path = tmp_path / "events.jsonl"
    path.write_text(bad + '\n{"offset": 2, "content": "ok"}\n', encoding="utf-8-sig")
    events, stats = replay.load_events(path)
    assert len(events) == 1 and events[0].content == "ok"
    assert stats.skipped == 1


def test_limits_cancellation_and_oversized_lines(tmp_path, monkeypatch):
    session = session_fixture(tmp_path)
    events, stats = replay.load_events(session.jsonl, limit=2)
    assert len(events) == 2 and stats.truncated
    hits, note = replay.search_sessions([session], "alice", threading.Event(), limit=1)
    assert len(hits) == 1 and "前 1 条" in note
    cancel = threading.Event()
    cancel.set()
    assert replay.load_events(session.jsonl, cancel)[0] == []
    monkeypatch.setattr(replay, "MAX_LINE", 50)
    session.jsonl.write_text("x" * 100 + '\n{"offset": 1, "content": "ok"}\n')
    events, stats = replay.load_events(session.jsonl)
    assert len(events) == 1 and stats.skipped == 1


def test_large_session_search_hit_loads_bounded_nearby_window(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text("\n".join(json.dumps({"offset": i, "content": str(i)}) for i in range(40)))
    events, stats = replay.load_events(path, limit=10, focus_offset=30)
    assert [e.offset for e in events] == list(range(25, 35))
    assert stats.truncated


def test_missing_search_file_and_empty_query_are_reported(tmp_path):
    session = session_fixture(tmp_path)
    session.jsonl.unlink()
    assert "不可读文件" in replay.search_sessions([session], "hi", threading.Event())[1]
    assert "请输入" in replay.search_sessions([session], " ", threading.Event())[1]


def test_metadata_survives_replacement_preserves_gaps_and_delay(tmp_path):
    session = session_fixture(tmp_path)
    videos = replay.current_videos(session)
    replay.save_metadata(session.prefix, "123", [
        {"file": p.name, "start": start, "duration": duration}
        for p, start, duration in zip(videos, (2, 12, 22), (5, 7, 3), strict=True)
    ], {"subtitle_delay": 1.5, "quality": "HD1", "cookie": "must-not-persist"})
    assert "cookie" not in replay.read_metadata(session.prefix)["recording"]
    videos[0].replace(videos[0].with_suffix(".mkv"))
    # 保留原始视频时生成的副本不应重复播放。
    videos[1].with_name(videos[1].stem + "_弹幕版.mkv").touch()
    timeline = replay.build_timeline(session, ffmpeg="unused")
    assert len(timeline.segments) == 3
    assert timeline.segments[0].path.suffix == ".mkv"
    assert timeline.locate(1) == (0, .5)
    assert timeline.locate(7) is None  # 断线空档
    assert timeline.locate(10.5) == (1, 0)
    assert timeline.locate(23.5) is None  # 末帧之后
    assert timeline.locate(0) is None  # 起录之前
    assert timeline.note == ""
    assert replay.saved_offsets(session.prefix, replay.current_videos(session)) == [2, 12, 22]


def test_legacy_fallback_natural_order_and_queue_timing(tmp_path, monkeypatch):
    session = session_fixture(tmp_path)
    monkeypatch.setattr(replay.subtitle, "probe", lambda *_: {"duration": 5})
    timeline = replay.build_timeline(session, ffmpeg="fake")
    assert [p.start for p in timeline.segments] == [0, 5, 10]
    assert timeline.segments[1].path.stem.endswith("_r2")
    assert "推算" in timeline.note
    task = SimpleNamespace(videos=[str(p.path) for p in timeline.segments],
                           offsets=[2, 10, 20], style={"delay": 2})
    exact = replay.build_timeline(session, task, ffmpeg="fake")
    assert [p.start for p in exact.segments] == [2, 10, 20]
    assert exact.delay == 2 and not exact.note


@pytest.mark.parametrize("raw", ["null", "[]", "{", '{"version": 2}',
                                '{"version": 1, "parts": null}',
                                '{"version": 1, "parts": [{"file": "x", "start": "bad"}]}'])
def test_invalid_metadata_falls_back(tmp_path, monkeypatch, raw):
    session = session_fixture(tmp_path)
    replay.metadata_path(session.prefix).write_text(raw)
    monkeypatch.setattr(replay.subtitle, "probe", lambda *_: {"duration": 5})
    assert "推算" in replay.build_timeline(session, ffmpeg="fake").note


def test_recorder_saves_timeline_even_without_subtitle_mux(tmp_path, monkeypatch):
    session = session_fixture(tmp_path, names=("", "_part001", "_r1"))
    videos = [session.prefix.with_suffix(".mp4"),
              session.prefix.with_name(session.prefix.name + "_part001.mp4"),
              session.prefix.with_name(session.prefix.name + "_r1.mp4")]
    recorder = object.__new__(Recorder)
    recorder.opts = Options(target="123", out_dir=tmp_path, embed_subtitle=False)
    recorder.web_rid = "123"
    recorder.ffmpeg = "fake"
    recorder._start_time = 100
    recorder._runs = [SimpleNamespace(zero_at=lambda: 102, files=lambda: videos[:2]),
                      SimpleNamespace(zero_at=lambda: 115, files=lambda: videos[2:])]
    probed = []

    def probe(_, path):
        probed.append(path)
        return {"duration": 5}

    monkeypatch.setattr(replay.subtitle, "probe", probe)
    recorder._save_replay_metadata(session.prefix)
    assert [p["start"] for p in replay.read_metadata(session.prefix)["parts"]] == [2, 7, 15]
    assert recorder._collect_parts() == (videos, [2, 7, 15])
    assert len(probed) == 3  # 不为封装重复探测所有分片。


def test_atomic_metadata_write_preserves_existing_on_failure(tmp_path, monkeypatch):
    prefix = tmp_path / "session"
    replay.save_metadata(prefix, "1", [], {})
    before = replay.metadata_path(prefix).read_bytes()
    monkeypatch.setattr(replay.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(OSError):
        replay.save_metadata(prefix, "2", [], {})
    assert replay.metadata_path(prefix).read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_no_timing_no_ffprobe_and_no_video_do_not_crash(tmp_path, monkeypatch):
    session = session_fixture(tmp_path)
    monkeypatch.setattr(replay.video, "find_ffmpeg", lambda: (_ for _ in ()).throw(RuntimeError()))
    timeline = replay.build_timeline(session)
    assert "不可读" in timeline.note and timeline.locate(1) is None
    for path in session.videos:
        path.unlink()
    assert "没有可用视频" in replay.build_timeline(session).note


def test_unreadable_legacy_part_does_not_misalign_following_parts(tmp_path, monkeypatch):
    session = session_fixture(tmp_path)
    monkeypatch.setattr(replay.subtitle, "probe", lambda _ff, path:
                        {} if path == session.prefix.with_suffix(".mp4") else {"duration": 5})
    timeline = replay.build_timeline(session, ffmpeg="fake")
    assert timeline.locate(1) is None
    assert [p.start for p in timeline.segments] == [0, -1, -1]


def test_dotted_title_video_danmaku_and_reconnect_keep_same_session(tmp_path, monkeypatch):
    from dylive import recorder as module
    from dylive.room import RoomInfo
    from dylive.writers import WriterGroup

    class FakeVideo(module.video.VideoRecorder):
        def start(self):
            self.out.touch()
            self.started_at = 102

        def stop(self):
            pass

    monkeypatch.setattr(module.video, "VideoRecorder", FakeVideo)
    monkeypatch.setattr(module.subtitle, "probe", lambda *_: {"duration": 4})
    info = RoomInfo("123", title="hello.world.mp4", nickname="主播", ok=True,
                    status=2, flv={"origin": "http://example.invalid/test"})
    monkeypatch.setattr(module.room, "fetch", lambda *_: info)
    recorder = object.__new__(Recorder)
    recorder.opts = Options(target="123", out_dir=tmp_path, retry_delay=0, show_console=False)
    recorder.web_rid, recorder.ffmpeg, recorder.session = "123", "fake", None
    recorder._stop, recorder._ended_by_anchor = threading.Event(), threading.Event()
    recorder._start_time = 100
    recorder._writers = WriterGroup()
    recorder.extra_writers = []
    recorder.hub = SimpleNamespace(attach=lambda *_: SimpleNamespace(start=lambda: None))
    recorder._watch_ffmpeg = lambda _: recorder._stop.set() if len(recorder._runs) == 2 else None
    prefix = recorder._make_prefix(info)
    recorder._start_danmaku(info, prefix)
    recorder._video_loop(info, prefix)
    recorder._writers.close()
    recorder._save_replay_metadata(prefix)
    sessions = scan_library(tmp_path)
    assert len(sessions) == 1 and sessions[0].prefix == prefix
    assert sessions[0].jsonl.name == prefix.name + ".jsonl"
    assert {p.name for p in sessions[0].videos} == {prefix.name + ".mp4", prefix.name + "_r1.mp4"}
    assert len(replay.read_metadata(prefix)["parts"]) == 2


def test_replay_finds_subtitle_copy_created_after_library_scan(tmp_path):
    session = session_fixture(tmp_path, names=("",))
    source = session.videos[0]
    output = source.with_name(source.stem + "_弹幕版.mkv")
    source.replace(output)
    assert replay.current_videos(session) == [output]
