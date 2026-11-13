"""国内模型下载与离线部署：不改动 Whisper 模型、不绕过校验。"""

import hashlib
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from types import ModuleType

import pytest

from dylive import transcription as speech


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.paths, "app_data_dir", lambda: tmp_path / "data")
    bodies = {"model_quant.onnx": b"weights", "tokens.json": b'["hello"]',
              "am.mvn": b"normalization", "config.yaml": b"frontend_conf: {}"}
    spec = next(s for s in speech.MODELS if s.key == "sensevoice-small")
    files = tuple((name, len(body), hashlib.sha256(body).hexdigest(), "a" * 40)
                  for name, body in bodies.items())
    spec = replace(spec, files=files, model_sha256=files[0][2])
    monkeypatch.setitem(speech._SPECS, spec.key, spec)
    return spec, bodies


def test_chinese_models_have_mainland_source_and_immutable_hashes():
    domestic = [m for m in speech.MODELS if m.engine != "whisper"]
    assert {m.key for m in domestic} == {"sensevoice-small", "paraformer-zh"}
    for model in domestic:
        assert model.repo_id.startswith("iic/")
        assert all(len(digest) == 64 and len(revision) == 40
                   for _, _, digest, revision in model.files)
    assert {m.key for m in speech.MODELS if m.engine == "whisper"} == {
        "tiny", "base", "small", "medium", "large-v3"}


def test_chinese_download_never_needs_huggingface(local_model, monkeypatch):
    spec, bodies = local_model
    requests = []

    class Response:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, _size):
            yield self.body

    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def get(self, url, **kwargs):
            assert url.startswith("https://modelscope.cn/")
            assert "/resolve/" + "a" * 40 + "/" in url
            requests.append(url)
            return Response(bodies[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(speech.requests, "Session", Session)
    directory = speech.download_model(spec.key)
    assert len(requests) == 4
    assert speech.is_model_downloaded(spec.key)
    assert speech._verify_model(spec.key, None) == directory


def test_offline_import_verifies_and_does_not_modify_source(local_model, tmp_path):
    spec, bodies = local_model
    source = tmp_path / "offline"
    source.mkdir()
    for name, body in bodies.items():
        (source / name).write_bytes(body)
    target = speech.import_model(spec.key, source)
    assert target != source and speech.is_model_downloaded(spec.key)
    assert (source / "model_quant.onnx").read_bytes() == b"weights"
    manifest = json.loads((target / speech._MANIFEST).read_text())
    assert manifest["revision"] == spec.revision
    speech.remove_model(spec.key)
    (source / "model_quant.onnx").write_bytes(b"changed")
    with pytest.raises(speech.TranscriptionError, match="校验不匹配"):
        speech.import_model(spec.key, source)
    assert not target.exists()
    assert not list(target.parent.glob(".*import*"))


def test_offline_import_cancel_does_not_publish(local_model, tmp_path):
    spec, bodies = local_model
    source = tmp_path / "offline"
    source.mkdir()
    for name, body in bodies.items():
        (source / name).write_bytes(body)
    with pytest.raises(speech.TranscriptionCancelled):
        speech.import_model(spec.key, source, cancelled=lambda: True)
    assert not speech.model_directory(spec.key).exists()


def test_chinese_decoders_apply_ctc_collapse_and_keep_paraformer_repetitions():
    np = pytest.importorskip("numpy")
    from dylive.china_asr import ChineseRecognizer
    recognizer = ChineseRecognizer.__new__(ChineseRecognizer)
    recognizer.tokens = ["<blank>", "你", "好", "<|zh|>"]
    recognizer._features = lambda _audio: np.zeros((1, 3, 80), dtype=np.float32)
    logits = np.eye(4)[np.array([3, 1, 1, 0, 2])][None]
    recognizer.session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name=s) for s in ["speech", "length", "lid", "itn"]],
        run=lambda *_args: [logits, np.array([5])])
    recognizer.engine = "sensevoice"
    assert recognizer._decode(np.zeros(16000), "zh") == "你好"
    recognizer.engine, recognizer.bias = "paraformer", 0
    recognizer.session.get_inputs = lambda: [SimpleNamespace(name=s) for s in ["speech", "length"]]
    assert recognizer._decode(np.zeros(16000), "zh") == "你你好"


def test_chinese_vad_segments_use_audio_time_and_skip_silence(monkeypatch):
    np = pytest.importorskip("numpy")
    # GUI 回归进程不载入 PyAV/ML 原生库；实际模型在独立进程集成检查。
    package = ModuleType("faster_whisper")
    vad = ModuleType("faster_whisper.vad")
    vad.VadOptions = lambda **kwargs: kwargs
    vad.get_speech_timestamps = lambda *_args: []
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    from dylive.china_asr import ChineseRecognizer
    recognizer = ChineseRecognizer.__new__(ChineseRecognizer)
    recognizer.engine = "paraformer"
    recognizer._decode = lambda *_args: "中文语音"
    monkeypatch.setattr(vad, "get_speech_timestamps", lambda *_args: [{"start": 16000, "end": 32000}])
    segments, info = recognizer.transcribe(np.zeros(48000), language="zh")
    result = list(segments)
    assert (result[0].start, result[0].end, result[0].text) == (1, 2, "中文语音")
    assert info.language == "zh"
    monkeypatch.setattr(vad, "get_speech_timestamps", lambda *_args: [])
    assert list(recognizer.transcribe(np.zeros(48000), language="zh")[0]) == []
    with pytest.raises(ValueError, match="普通话"):
        recognizer.transcribe(np.zeros(48000), language="en")
