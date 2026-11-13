"""本地语音下载完整性、取消边界和 SRT 发布；无需真实模型或联网。"""

import hashlib
import json
import shutil
import subprocess
import threading
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dylive import transcription as speech


@pytest.fixture
def model_store(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.paths, "app_data_dir", lambda: tmp_path / "data")
    return tmp_path / "data" / "speech-models"


class Response:
    def __init__(self, payload=None, body=b""):
        self.payload, self.body = payload, body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, _):
        middle = max(1, len(self.body) // 2)
        yield self.body[:middle]
        yield self.body[middle:]


@pytest.fixture
def fake_upstream(monkeypatch, model_store):
    files = {"config.json": b'{"model_type":"Whisper"}', "model.bin": b"weights1234",
             "tokenizer.json": b'{"version":"1.0"}', "vocabulary.txt": b"tokens\n"}
    spec = replace(speech.MODELS[0], model_sha256=hashlib.sha256(files["model.bin"]).hexdigest())
    monkeypatch.setitem(speech._SPECS, "tiny", spec)
    siblings = []
    for name, body in files.items():
        entry = {"rfilename": name, "size": len(body),
                 "blobId": hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()}
        if name == "model.bin":
            entry["lfs"] = {"sha256": spec.model_sha256}
        siblings.append(entry)
    metadata = {"sha": spec.revision, "siblings": siblings}
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if "/api/models/" in url:
                return Response(payload=metadata)
            return Response(body=files[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(speech.requests, "Session", Session)
    return files, metadata, calls


def test_status_does_not_create_model_directories(model_store):
    assert not speech.is_model_downloaded("tiny")
    assert not speech.model_status("tiny")
    assert not model_store.exists()
    with pytest.raises(speech.TranscriptionError, match="未知"):
        speech.model_directory("../../outside")


def test_download_verifies_pinned_files_and_is_repeatable(fake_upstream, model_store):
    _, _, calls = fake_upstream
    progress = []
    directory = speech.download_model("tiny", progress=lambda f, message: progress.append(f))
    assert directory == model_store / "tiny"
    assert speech.is_model_downloaded("tiny")
    manifest = json.loads((directory / speech._MANIFEST).read_text())
    assert manifest["revision"] == speech.MODELS[0].revision
    assert len(manifest["files"]) == 4
    assert progress[0] == 0 and progress[-1] == 1
    assert all(0 <= f <= 1 for f in progress)
    assert all(speech.MODELS[0].revision in url for url, _ in calls)
    before = len(calls)
    assert speech.download_model("tiny") == directory
    assert len(calls) == before
    speech.remove_model("tiny")
    assert not speech.is_model_downloaded("tiny")


@pytest.mark.parametrize("file_name", ["model.bin", "tokenizer.json"])
def test_corrupt_download_does_not_publish_model(fake_upstream, model_store, file_name):
    files, _, _ = fake_upstream
    original = files[file_name]
    files[file_name] = b"X" + original[1:]
    with pytest.raises(speech.TranscriptionError, match="校验失败"):
        speech.download_model("tiny")
    assert not speech.is_model_downloaded("tiny")
    assert not list(model_store.iterdir())


def test_wrong_upstream_revision_is_rejected(fake_upstream, model_store):
    _, metadata, _ = fake_upstream
    metadata["sha"] = "wrong"
    with pytest.raises(speech.TranscriptionError, match="版本校验"):
        speech.download_model("tiny")
    assert not list(model_store.iterdir())


def test_download_cancel_cleans_partial_model(fake_upstream, model_store):
    stop = threading.Event()
    def on_progress(fraction, message):
        if fraction > 0:
            stop.set()
    with pytest.raises(speech.TranscriptionCancelled):
        speech.download_model("tiny", progress=on_progress, cancelled=stop.is_set)
    assert not list(model_store.iterdir())


def test_missing_and_locally_corrupt_model_fail_before_inference(fake_upstream):
    with pytest.raises(speech.TranscriptionError, match="先到"):
        speech._verify_model("tiny", None)
    directory = speech.download_model("tiny")
    path = directory / "model.bin"
    path.write_bytes(b"X" + path.read_bytes()[1:])
    # Lightweight UI status checks size; inference always rechecks the actual content hash.
    assert speech.is_model_downloaded("tiny")
    with pytest.raises(speech.TranscriptionError, match="已损坏"):
        speech._verify_model("tiny", None)


def test_active_model_cannot_be_deleted_or_downloaded(fake_upstream):
    speech.download_model("tiny")
    with speech._model_lock("tiny"):
        with pytest.raises(speech.TranscriptionError, match="正在"):
            speech.remove_model("tiny")
        with pytest.raises(speech.TranscriptionError, match="正在"):
            speech.download_model("tiny")


def test_atomic_srt_preserves_existing_and_formats_time(tmp_path):
    out = tmp_path / "speech.srt"
    segments = (speech.Segment(0.001, 1.9996, "第一句"),
                speech.Segment(3600.05, 3601.1, "Next sentence"))
    speech._write_srt(segments, out, False, None)
    text = out.read_text()
    assert "00:00:00,001 --> 00:00:02,000" in text
    assert "01:00:00,050 --> 01:00:01,100" in text
    with pytest.raises(speech.TranscriptionError, match="已存在"):
        speech._write_srt((speech.Segment(0, 1, "overwrite"),), out, False, None)
    assert out.read_text() == text
    assert not list(tmp_path.glob("*.part"))


@pytest.fixture
def fake_inference(monkeypatch, tmp_path):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"untouched video")
    monkeypatch.setattr(speech, "dependency_available", lambda: True)
    monkeypatch.setattr(speech, "_verify_model", lambda *_: tmp_path)
    monkeypatch.setattr(speech.video, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(speech, "_extract_audio", lambda *_: 4.0)
    monkeypatch.setattr(speech, "_audio_windows", lambda *_: iter([(0.0, 4.0, b"pcm")]))
    monkeypatch.setattr(speech, "_audio_array", lambda pcm: pcm)
    return source


def test_transcription_outputs_local_time_srt_without_altering_video(fake_inference,
                                                                   monkeypatch):
    source = fake_inference
    args = []
    class Engine:
        def transcribe(self, path, **kwargs):
            args.append(kwargs)
            return iter([SimpleNamespace(start=0.5, end=2.0, text=" 你好\n世界 "),
                         SimpleNamespace(start=2.0, end=8.0, text="第二句")]), SimpleNamespace(language="zh")
    monkeypatch.setattr(speech, "_load_model", lambda _: Engine())
    result = speech.transcribe_video(source, model="tiny", language="auto")
    assert result.language == "zh" and result.duration == 4
    assert result.segments[0].text == "你好 世界"
    assert result.segments[-1].end == 4
    assert "00:00:00,500 --> 00:00:02,000" in result.srt_path.read_text()
    assert source.read_bytes() == b"untouched video"
    assert args[0]["language"] is None and args[0]["vad_filter"] is True


def test_silent_input_is_non_failure_without_publishing_empty_srt(fake_inference,
                                                                monkeypatch):
    class Engine:
        def transcribe(self, *_, **__):
            return iter([]), SimpleNamespace(language="zh")
    monkeypatch.setattr(speech, "_load_model", lambda _: Engine())
    result = speech.transcribe_video(fake_inference, model="tiny")
    assert not result.segments
    assert not result.srt_path.exists()


def test_cancel_during_inference_keeps_preexisting_srt(fake_inference, monkeypatch):
    stop = threading.Event()
    target = fake_inference.with_suffix(".srt")
    target.write_text("user subtitle")
    class Engine:
        def transcribe(self, *_, **__):
            def segments():
                yield SimpleNamespace(start=0, end=1, text="first")
                stop.set()
                yield SimpleNamespace(start=1, end=2, text="second")
            return segments(), SimpleNamespace(language="zh")
    monkeypatch.setattr(speech, "_load_model", lambda _: Engine())
    with pytest.raises(speech.TranscriptionCancelled):
        speech.transcribe_video(fake_inference, model="tiny", output=target,
                                overwrite=True, cancelled=stop.is_set)
    assert target.read_text() == "user subtitle"


def test_cannot_overwrite_source_even_when_requested(fake_inference):
    with pytest.raises(speech.TranscriptionError, match="不能覆盖源视频"):
        speech.transcribe_video(fake_inference, model="tiny", output=fake_inference,
                                overwrite=True)


def test_pcm_reader_bounds_memory_and_keeps_final_partial_window(tmp_path, monkeypatch):
    path = tmp_path / "long-audio.wav"
    rate = 16000
    # 三个很短的测试窗口证明分块边界与尾块；不创建几个小时的测试音频。
    monkeypatch.setattr(speech, "_AUDIO_WINDOW_SECONDS", 1)
    samples = b"\x01\x00" * (rate * 2 + rate // 4)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples)
    chunks = list(speech._audio_windows(path, None))
    assert [(start, length) for start, length, _ in chunks] == [(0, 1), (1, 1), (2, .25)]
    assert all(len(pcm) <= rate * 2 for _, _, pcm in chunks)
    assert b"".join(pcm for _, _, pcm in chunks) == samples
    stop = threading.Event()
    iterator = speech._audio_windows(path, stop.is_set)
    next(iterator)
    stop.set()
    with pytest.raises(speech.TranscriptionCancelled):
        next(iterator)


def test_multiple_windows_rebase_trim_and_carry_detected_language(fake_inference,
                                                               monkeypatch):
    monkeypatch.setattr(speech, "_extract_audio", lambda *_: 12.0)
    monkeypatch.setattr(speech, "_audio_windows", lambda *_: iter([
        (0.0, 5.0, b"silent"), (5.0, 5.0, b"first speech"),
        (10.0, 2.0, b"last speech")]))
    calls, loads, progress = [], [], []
    class Engine:
        def transcribe(self, audio, **kwargs):
            calls.append((audio, kwargs["language"]))
            if audio == b"silent":
                return iter([]), SimpleNamespace(language="en")
            return iter([SimpleNamespace(start=-.1, end=9, text=audio.decode())]), SimpleNamespace(language="zh")
    def load(_):
        loads.append(True)
        return Engine()
    monkeypatch.setattr(speech, "_load_model", load)
    result = speech.transcribe_video(fake_inference, model="tiny", language="auto",
                                    progress=lambda f, _: progress.append(f))
    assert len(loads) == 1
    assert calls == [(b"silent", None), (b"first speech", None), (b"last speech", "zh")]
    assert [(s.start, s.end) for s in result.segments] == [(5, 10), (10, 12)]
    assert result.language == "zh"
    assert progress[-1] == 1 and all(f < 1 for f in progress[:-1])
    text = result.srt_path.read_text()
    assert "00:00:05,000 --> 00:00:10,000" in text
    assert "00:00:10,000 --> 00:00:12,000" in text


def test_cancellation_between_windows_never_publishes_partial_srt(fake_inference,
                                                              monkeypatch):
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(speech, "_audio_windows", lambda *_: iter([
        (0.0, 2.0, b"first"), (2.0, 2.0, b"second")]))
    class Engine:
        def transcribe(self, audio, **kwargs):
            calls.append(audio)
            return iter([SimpleNamespace(start=0, end=2, text="first")]), SimpleNamespace(language="zh")
    monkeypatch.setattr(speech, "_load_model", lambda _: Engine())
    def progress(fraction, _):
        if fraction >= .53:
            stop.set()
    with pytest.raises(speech.TranscriptionCancelled):
        speech.transcribe_video(fake_inference, model="tiny", cancelled=stop.is_set,
                                progress=progress)
    assert calls == [b"first"]
    assert not fake_inference.with_name(fake_inference.stem + ".speech.srt").exists()


def test_real_audio_extraction_rejects_video_without_audio(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg 未安装")
    source = tmp_path / "silent-video.mkv"
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=5", "-t", "0.2", "-an",
        "-c:v", "ffv1", str(source)], capture_output=True)
    assert result.returncode == 0, result.stderr
    with pytest.raises(speech.TranscriptionError, match="有效音轨"):
        speech._extract_audio(ffmpeg, source, tmp_path / "audio.wav", None)
