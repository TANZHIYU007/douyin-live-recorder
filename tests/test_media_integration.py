"""可选的真实媒体回归：设置 LUMINA_TEST_FFMPEG 后启用，不访问外部直播间。"""

import functools
import json
import os
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dylive import subtitle
from dylive.paths import NO_WINDOW
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
