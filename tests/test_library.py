"""本地录像库按录制前缀归档，不依赖额外数据库。"""

from pathlib import Path

from dylive.library import recording_prefix, scan_library


def test_recording_prefix_recognizes_reconnect_segments_and_subtitle_copy(tmp_path):
    folder = tmp_path / "主播"
    prefix = folder / "20261009_203000_标题[测试]"

    assert recording_prefix(Path(str(prefix) + ".mp4")) == prefix
    assert recording_prefix(Path(str(prefix) + "_r2.mp4")) == prefix
    assert recording_prefix(Path(str(prefix) + "_r2_part003.mp4")) == prefix
    assert recording_prefix(Path(str(prefix) + "_part001.ts")) == prefix
    assert recording_prefix(Path(str(prefix) + "_弹幕版.mkv")) == prefix


def test_scan_library_groups_one_session_and_ignores_unrelated_files(tmp_path):
    folder = tmp_path / "主播甲"
    folder.mkdir()
    prefix = folder / "20261009_203000_今晚聊天"
    outputs = [
        Path(str(prefix) + ".mp4"),
        Path(str(prefix) + "_r1.mp4"),
        Path(str(prefix) + "_r1_part001.mp4"),
        Path(str(prefix) + ".jsonl"),
        Path(str(prefix) + ".xml"),
        Path(str(prefix) + "_弹幕版.mkv"),
    ]
    for index, path in enumerate(outputs, 1):
        path.write_bytes(b"x" * index)
    (folder / "说明.txt").write_text("ignore", encoding="utf-8")

    sessions = scan_library(tmp_path)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.prefix == prefix
    assert session.streamer == "主播甲"
    assert session.title == "今晚聊天"
    assert len(session.videos) == 4
    assert session.jsonl == Path(str(prefix) + ".jsonl")
    assert session.xml == Path(str(prefix) + ".xml")
    assert session.total_bytes == sum(range(1, len(outputs) + 1))


def test_scan_library_returns_newest_session_first(tmp_path):
    folder = tmp_path / "主播"
    folder.mkdir()
    for name in ("20261008_203000_旧场", "20261009_090000_新场"):
        (folder / (name + ".mp4")).write_bytes(b"video")

    sessions = scan_library(tmp_path)

    assert [session.title for session in sessions] == ["新场", "旧场"]
