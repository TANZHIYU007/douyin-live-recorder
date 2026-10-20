"""可选的真实媒体回归：设置 LUMINA_TEST_FFMPEG 后启用，不访问外部直播间。"""

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dylive import subtitle
from dylive.paths import NO_WINDOW
from dylive.processing import COMPLETED, FAILED, PostProcessQueue
from dylive.video import CONTAINERS, VideoRecorder


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=30, **NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    binary = os.environ.get("LUMINA_TEST_FFMPEG")
    if not binary:
        pytest.skip("设置 LUMINA_TEST_FFMPEG 可启用真实录制与封装测试")
    ffmpeg = Path(binary)
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    assert ffmpeg.is_file() and ffprobe.is_file()
    folder = tmp_path_factory.mktemp("media")
    source = folder / "source.mp4"
    run(ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", source)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        QuietHandler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ffmpeg, ffprobe, source, "http://127.0.0.1:%d/source.mp4" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def inspect_media(ffmpeg, ffprobe, path):
    info = json.loads(run(ffprobe, "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path))
    kinds = {stream["codec_type"] for stream in info["streams"]}
    assert {"video", "audio"} <= kinds
    assert float(info["format"]["duration"]) > 0
    run(ffmpeg, "-v", "error", "-i", path, "-map", "0:v:0", "-map", "0:a:0",
        "-f", "null", "-")
    return info


@pytest.mark.parametrize("container", CONTAINERS)
@pytest.mark.parametrize("segment", [0, 2])
def test_real_recording_all_containers(media, tmp_path, container, segment):
    ffmpeg, ffprobe, _, url = media
    recorder = VideoRecorder(str(ffmpeg), url, tmp_path / "录像[测试]",
                             container=container, segment_seconds=segment)
    try:
        recorder.start()
        assert recorder.proc.wait(timeout=30) == 0
        assert recorder.note_first_data()
    finally:
        recorder.stop()
        if recorder._stderr_thread:
            recorder._stderr_thread.join(3)
        if recorder.proc and recorder.proc.stderr:
            recorder.proc.stderr.close()
    files = recorder.files()
    assert len(files) >= 2 if segment else len(files) == 1
    for path in files:
        inspect_media(ffmpeg, ffprobe, path)


def test_real_qt_replay_decodes_seeks_and_advances_segments(media, qapp, tmp_path):
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from dylive.library import scan_library
    from dylive.replay import save_metadata
    from dylive.ui.replay import ReplayDialog
    ffmpeg, _, source, _ = media
    prefix = tmp_path / "20261020_200000_回放"
    first, second = prefix.with_suffix(".mp4"), tmp_path / (prefix.name + "_r1.mp4")
    for path in (first, second):
        shutil.copyfile(source, path)
    prefix.with_suffix(".jsonl").write_text('\n'.join(json.dumps({
        "offset": offset, "user_name": "测试", "content": content, "kind": "chat"})
        for offset, content in [(2.5, "第一段"), (10.5, "第二段")]), encoding="utf-8")
    save_metadata(prefix, "123", [{"file": first.name, "start": 2, "duration": 4},
                                  {"file": second.name, "start": 10, "duration": 4}], {})
    dialog = ReplayDialog(scan_library(tmp_path)[0], ffmpeg=str(ffmpeg))
    frames = []
    dialog.video.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
    dialog.show()
    dialog.audio.setMuted(True)

    def wait(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            qapp.processEvents()
            if predicate():
                return
            QTest.qWait(20)
        assert predicate(), dialog.status.text()

    try:
        wait(lambda: dialog.segment_index == 0 and dialog.player.isSeekable())
        dialog.seek_event(3.5)
        wait(lambda: dialog.player.position() >= 1500 and any(frames))
        dialog.player.pause()
        assert "第一段" in dialog.chat.text() and "第二段" not in dialog.chat.text()
        dialog.seek_event(10.5)
        wait(lambda: dialog.segment_index == 1 and dialog.player.isSeekable()
             and dialog.player.position() >= 500 and "第二段" in dialog.chat.text())
        dialog.player.pause()
        dialog.seek_event(6.5)  # 空档不跳到错误画面，也不改变当前分片。
        assert dialog.segment_index == 1 and "没有对应画面" in dialog.status.text()
        dialog._choose_segment(0, 3.7)
        wait(lambda: dialog.segment_index == 1)  # 当前片段结束后自动接下一片段。
        assert dialog.player.error() == QMediaPlayer.NoError
    finally:
        dialog.reject()
    assert dialog.player.source().isEmpty()


@pytest.mark.parametrize("mode", [subtitle.CHAT, subtitle.SCROLL])
@pytest.mark.parametrize("replace", [False, True])
def test_real_subtitle_mux_and_replacement(media, tmp_path, mode, replace):
    ffmpeg, ffprobe, source, _ = media
    video = tmp_path / "录像.mp4"
    shutil.copyfile(source, video)
    jsonl = tmp_path / "录像.jsonl"
    jsonl.write_text("\n".join(json.dumps({"kind": "chat", "offset": t,
        "content": "测试弹幕", "user_name": "用户"}, ensure_ascii=False)
        for t in (0.5, 2.0)), encoding="utf-8")
    results = subtitle.process(str(ffmpeg), jsonl, [video],
                               style=subtitle.Style(mode=mode, size=16),
                               replace=replace, keep_ass=True)
    assert len(results) == 1
    assert video.exists() is not replace
    assert results[0].name == ("录像.mkv" if replace else "录像_弹幕版.mkv")
    info = inspect_media(ffmpeg, ffprobe, results[0])
    assert any(s["codec_type"] == "subtitle" and s["codec_name"] == "ass"
               for s in info["streams"])
    ass = video.with_suffix(".ass").read_text(encoding="utf-8-sig")
    assert "测试弹幕" in ass
    if mode == subtitle.CHAT:
        assert "0:00:00.50,0:00:02.00" in ass
        assert "0:00:02.00,0:00:04.00" in ass


def test_real_queue_partial_failure_and_restart_retry(media, tmp_path):
    ffmpeg, ffprobe, source, _url = media
    prefix = tmp_path / "20261020_200000_测试"
    first = prefix.with_suffix(".mp4")
    second = prefix.with_name(prefix.name + "_part001.mp4")
    shutil.copyfile(source, first)
    second.write_bytes(b"broken video")
    jsonl = prefix.with_suffix(".jsonl")
    jsonl.write_text("\n".join(json.dumps({"kind": "chat", "content": "测试弹幕", "offset": t})
                               for t in (0.5, 4.5)), encoding="utf-8")
    state = tmp_path / "queue.json"
    queue = PostProcessQueue(state, ffmpeg_provider=lambda: str(ffmpeg))
    try:
        task = queue.enqueue_subtitles(prefix, jsonl, [first, second], offsets=[0, 4],
                                       style=subtitle.Style(mode=subtitle.CHAT, size=16))
        assert queue.wait_until_idle(20)
        failed = queue.snapshot()[0]
        assert failed.status == FAILED
        assert second.name in failed.error
        assert "退出码" in failed.error
        assert failed.results == [str(first.with_suffix(".mkv"))]
        assert not first.exists()
        assert second.read_bytes() == b"broken video"
    finally:
        queue.shutdown()

    shutil.copyfile(source, second)
    restored = PostProcessQueue(state, ffmpeg_provider=lambda: str(ffmpeg))
    try:
        assert restored.retry(task.id)
        assert restored.wait_until_idle(20)
        done = restored.snapshot()[0]
        assert done.status == COMPLETED
        assert done.attempts == 2
        assert len(done.results) == 2
        for result in done.results:
            info = inspect_media(ffmpeg, ffprobe, result)
            assert any(stream["codec_type"] == "subtitle" for stream in info["streams"])
        text = run(ffmpeg, "-v", "error", "-i", second.with_suffix(".mkv"),
                   "-map", "0:s:0", "-c:s", "ass", "-f", "ass", "-").decode("utf-8")
        # FFmpeg 版本不同，AAC 预滚可能让封装后的所有轨道整体平移约 23ms。
        # 校验相对视频首帧的时间；ASS 只精确到百分之一秒，不能比较固定字符串。
        timing = json.loads(run(ffprobe, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=start_time", "-of", "json",
                                second.with_suffix(".mkv")))
        video_start = float(timing["streams"][0]["start_time"])
        starts = []
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                h, m, s = map(float, line.split(",")[1].split(":"))
                starts.append(h * 3600 + m * 60 + s - video_start)
        assert starts
        assert min(starts) == pytest.approx(0, abs=0.02)  # 继承上一片的消息。
        assert max(starts) == pytest.approx(0.5, abs=0.02)  # 4.5 秒减去片段起点 4 秒。
        assert not second.exists()
    finally:
        restored.shutdown()
