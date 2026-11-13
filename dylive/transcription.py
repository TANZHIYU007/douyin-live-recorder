"""按需下载本地语音模型，并把单个视频转成独立的语音 SRT。

录制不依赖这个模块；仅在用户主动转写时才导入 faster-whisper。模型下载和
推理分开，推理只接受本地完整模型，不会悄悄联网。所有时间都相对当前视频。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from . import paths, video

Progress = Optional[Callable[[float, str], None]]
Cancelled = Optional[Callable[[], bool]]


class TranscriptionError(RuntimeError):
    """可直接显示给用户的下载或转写错误。"""


class TranscriptionCancelled(TranscriptionError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    repo_id: str
    revision: str
    size_mb: int
    description: str
    model_sha256: str
    engine: str = "whisper"
    files: tuple = ()  # (name, size, sha256, immutable revision)


# 固定上游提交和权重散列，避免同名模型更新后悄悄换掉已选择的模型。
# 来源：对应 Hugging Face 仓库的官方模型元数据（2026-11-09 核对）。
MODELS = (
    ModelSpec("tiny", "Tiny · 最快", "Systran/faster-whisper-tiny",
              "d90ca5fe260221311c53c58e660288d3deb8d356", 79,
              "多语言；适合试用和较清晰语音，准确度较低，内存建议 1 GB 以上。",
              "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
    ModelSpec("base", "Base · 轻量", "Systran/faster-whisper-base",
              "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66", 148,
              "多语言；速度较快，适合普通电脑，内存建议 1 GB 以上。",
              "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
    ModelSpec("small", "Small · 推荐", "Systran/faster-whisper-small",
              "536b0662742c02347bc0e980a01041f333bce120", 487,
              "多语言；兼顾中文识别和速度，内存建议 2 GB 以上。",
              "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
    ModelSpec("medium", "Medium · 更准确", "Systran/faster-whisper-medium",
              "08e178d48790749d25932bbc082711ddcfdfbc4f", 1531,
              "多语言；中文和复杂语音通常更准确，CPU 较慢，内存建议 4 GB 以上。",
              "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae"),
    ModelSpec("large-v3", "Large v3 · 高精度", "Systran/faster-whisper-large-v3",
              "edaa852ec7e145841d8ffdb056a99866b5f0a478", 3092,
              "多语言；适合重视准确度且能等待的用户，CPU 很慢，内存建议 8 GB 以上。",
              "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
)

# 阿里模型的官方魔搭 ONNX 发布；每个文件固定提交及 SHA-256，不依赖境外镜像。
MODELS += (
    ModelSpec("sensevoice-small", "SenseVoice Small · 阿里多语言", "iic/SenseVoiceSmall-onnx",
              "4e95991ddb34c70e9b94f026d62ac82a9d941ae1", 242,
              "阿里通义语音模型；支持中英日韩粤语，CPU 运行，下载源为魔搭 ModelScope。",
              "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822",
              "sensevoice", (
                  ("model_quant.onnx", 241216270, "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822", "4e95991ddb34c70e9b94f026d62ac82a9d941ae1"),
                  ("tokens.json", 352064, "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97", "4e95991ddb34c70e9b94f026d62ac82a9d941ae1"),
                  ("config.yaml", 1855, "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19", "4e95991ddb34c70e9b94f026d62ac82a9d941ae1"),
                  ("am.mvn", 11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5", "4e95991ddb34c70e9b94f026d62ac82a9d941ae1"),
              )),
    ModelSpec("paraformer-zh", "Paraformer · 阿里中文", "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx",
              "7162433de474912574f9d03122263776db5b046a", 239,
              "阿里达摩院中文模型；面向普通话，CPU 运行，下载源为魔搭 ModelScope。",
              "c3a06538f867c8e3d9bb9c9e1b44b126a50d732862f6c428b1b5896ee9be65c5",
              "paraformer", (
                  ("model_quant.onnx", 238380216, "c3a06538f867c8e3d9bb9c9e1b44b126a50d732862f6c428b1b5896ee9be65c5", "7162433de474912574f9d03122263776db5b046a"),
                  ("tokens.json", 93676, "2b20c2b12572d682afff84ce1c8d560f67b8b32a4c1f21567411d141ed352127", "81fccf72866ff54325d7376567e0ebc5947862f3"),
                  ("config.yaml", 2509, "bfd424b9f61846aac63591e96fe24d66b35f4643bddcb4b9bb759db8f987ab79", "81fccf72866ff54325d7376567e0ebc5947862f3"),
                  ("am.mvn", 11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5", "4e7b8ca7e8694907b38701fa7b142246994e7793"),
              )),
)

_SPECS = {spec.key: spec for spec in MODELS}
_LOCKS = {spec.key: threading.Lock() for spec in MODELS}
_MANIFEST = "lumina-model.json"
_CHUNK_SIZE = 1024 * 1024
_AUDIO_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    segments: tuple[Segment, ...]
    language: str
    duration: float
    srt_path: Path


def _spec(key: str) -> ModelSpec:
    try:
        return _SPECS[key]
    except KeyError as exc:
        raise TranscriptionError("未知语音模型：%s" % key) from exc


def model_directory(key: str) -> Path:
    _spec(key)
    return paths.app_data_dir() / "speech-models" / key


def _filenames(spec: ModelSpec) -> tuple[str, ...]:
    if spec.files:
        return tuple(entry[0] for entry in spec.files)
    if spec.key == "large-v3":
        return ("config.json", "model.bin", "tokenizer.json", "vocabulary.json",
                "preprocessor_config.json")
    return ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def dependency_available() -> bool:
    """只查模块是否已安装；设置窗口不会因此载入推理库。"""
    return importlib.util.find_spec("faster_whisper") is not None


def model_dependency_available(key: str) -> bool:
    if _spec(key).engine == "whisper":
        return dependency_available()
    return dependency_available() and all(importlib.util.find_spec(name) is not None
                                          for name in ("onnxruntime", "kaldi_native_fbank", "yaml"))


def _local_manifest(key: str) -> Optional[dict]:
    spec = _spec(key)
    directory = model_directory(key)
    try:
        if directory.is_symlink():
            return None
        manifest = json.loads((directory / _MANIFEST).read_text(encoding="utf-8"))
        if (manifest.get("repo_id") != spec.repo_id
                or manifest.get("revision") != spec.revision
                or not isinstance(manifest.get("files"), dict)):
            return None
        for name in _filenames(spec):
            entry = manifest["files"].get(name)
            path = directory / name
            if (not isinstance(entry, dict) or not isinstance(entry.get("size"), int)
                    or entry["size"] <= 0 or not isinstance(entry.get("sha256"), str)
                    or len(entry["sha256"]) != 64 or path.is_symlink()
                    or not path.is_file() or path.stat().st_size != entry["size"]):
                return None
        weight = "model.bin" if spec.engine == "whisper" else "model_quant.onnx"
        if manifest["files"][weight]["sha256"] != spec.model_sha256:
            return None
        if any(manifest["files"][name]["sha256"] != digest for name, _, digest, _ in spec.files):
            return None
        return manifest
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def is_model_downloaded(key: str) -> bool:
    """轻量状态检查：不建目录、不联网、不导入机器学习库。"""
    return _local_manifest(key) is not None


def model_status(key: str) -> bool:
    """处理队列使用的就绪状态，与设置页的轻量检查保持一致。"""
    return is_model_downloaded(key)


def _check_cancel(cancelled: Cancelled) -> None:
    if cancelled and cancelled():
        raise TranscriptionCancelled("操作已取消")


def _notify(progress: Progress, fraction: float, message: str) -> None:
    if progress:
        progress(max(0.0, min(1.0, fraction)), message)


@contextmanager
def _model_lock(key: str):
    _spec(key)
    lock = _LOCKS[key]
    if not lock.acquire(blocking=False):
        raise TranscriptionError("此模型正在下载或使用，请稍后再试")
    try:
        yield
    finally:
        lock.release()


def _remote_files(spec: ModelSpec, session: requests.Session) -> list[dict]:
    if spec.files:
        return [{"name": name, "size": size, "sha256": digest, "revision": rev}
                for name, size, digest, rev in spec.files]
    url = "https://huggingface.co/api/models/%s/revision/%s" % (
        spec.repo_id, spec.revision)
    with session.get(url, params={"blobs": "true"}, timeout=(10, 30)) as response:
        response.raise_for_status()
        data = response.json()
    if data.get("sha") != spec.revision:
        raise TranscriptionError("模型版本校验失败，请稍后重试")
    siblings = {entry.get("rfilename"): entry for entry in data.get("siblings", [])
                if isinstance(entry, dict)}
    files = []
    for name in _filenames(spec):
        entry = siblings.get(name, {})
        size = entry.get("size")
        lfs = entry.get("lfs") or {}
        digest = lfs.get("sha256")
        blob_id = entry.get("blobId")
        if (not isinstance(size, int) or size <= 0
                or (not digest and not blob_id)):
            raise TranscriptionError("上游模型文件信息不完整：%s" % name)
        if name == "model.bin" and digest != spec.model_sha256:
            raise TranscriptionError("上游模型权重校验信息不匹配")
        files.append({"name": name, "size": size,
                      "sha256": digest, "blob_id": blob_id})
    return files


def download_model(key: str, *, progress: Progress = None,
                   cancelled: Cancelled = None) -> Path:
    """显式下载固定版本，核对上游散列后才让模型显示为可用。"""
    spec = _spec(key)
    _check_cancel(cancelled)
    with _model_lock(key):
        destination = model_directory(key)
        if is_model_downloaded(key):
            _notify(progress, 1.0, "模型已下载")
            return destination
        if destination.exists():
            raise TranscriptionError("模型目录不完整，请先删除此模型再重新下载")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".%s-download-" % key,
                                        dir=destination.parent))
        try:
            _notify(progress, 0.0, "正在获取模型文件信息")
            with requests.Session() as session:
                files = _remote_files(spec, session)
                total = sum(entry["size"] for entry in files)
                downloaded = 0
                manifest = {"repo_id": spec.repo_id, "revision": spec.revision,
                            "files": {}}
                for entry in files:
                    _check_cancel(cancelled)
                    name, size = entry["name"], entry["size"]
                    sha256 = hashlib.sha256()
                    git_hash = hashlib.sha1(b"blob %d\0" % size)
                    received = 0
                    if spec.files:
                        url = "https://modelscope.cn/models/%s/resolve/%s/%s" % (
                            spec.repo_id, entry["revision"], name)
                        params = {}
                    else:
                        url = "https://huggingface.co/%s/resolve/%s/%s" % (
                            spec.repo_id, spec.revision, name)
                        params = {}
                    with session.get(url, params=params, stream=True, timeout=(15, 45)) as response:
                        response.raise_for_status()
                        with (staging / name).open("wb") as fh:
                            for chunk in response.iter_content(_CHUNK_SIZE):
                                _check_cancel(cancelled)
                                if not chunk:
                                    continue
                                received += len(chunk)
                                if received > size:
                                    raise TranscriptionError("模型文件大小不匹配：%s" % name)
                                fh.write(chunk)
                                sha256.update(chunk)
                                git_hash.update(chunk)
                                _notify(progress, (downloaded + received) / total,
                                        "正在下载 %s（%.1f / %.1f MB）" % (
                                            spec.label, (downloaded + received) / 1e6,
                                            total / 1e6))
                    digest = sha256.hexdigest()
                    if (received != size
                            or (entry["sha256"] and digest != entry["sha256"])
                            or (not entry["sha256"]
                                and git_hash.hexdigest() != entry["blob_id"])):
                        raise TranscriptionError("模型文件校验失败：%s；请重新下载" % name)
                    downloaded += received
                    manifest["files"][name] = {"size": size, "sha256": digest}
            _check_cancel(cancelled)
            (staging / _MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False,
                                                       indent=2), encoding="utf-8")
            staging.rename(destination)
            _notify(progress, 1.0, "模型下载并校验完成")
            return destination
        except requests.RequestException as exc:
            # 下载 URL 的查询部分可能带临时签名，不把完整远端异常写进界面或队列。
            raise TranscriptionError("模型下载失败，请检查网络后重试（%s）" %
                                     type(exc).__name__) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise TranscriptionError("模型下载失败：%s" % exc) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def remove_model(key: str) -> None:
    with _model_lock(key):
        directory = model_directory(key)
        if directory.is_symlink():
            raise TranscriptionError("模型目录是符号链接，不能自动删除")
        try:
            if directory.exists():
                shutil.rmtree(directory)
        except OSError as exc:
            raise TranscriptionError("无法删除模型，请确认没有转写任务正在使用它") from exc


def import_model(key: str, folder: Path, *, progress: Progress = None,
                 cancelled: Cancelled = None) -> Path:
    """从用户选择的本地目录复制模型，权重须符合目录中所列的固定版本。"""
    spec, folder = _spec(key), Path(folder)
    required = _filenames(spec)
    expected = {name: digest for name, _, digest, _ in spec.files}
    expected["model.bin" if spec.engine == "whisper" else "model_quant.onnx"] = spec.model_sha256
    with _model_lock(key):
        target = model_directory(key)
        if target.exists():
            raise TranscriptionError("本地已有此模型，请先删除后再导入")
        sources = [folder / name for name in required]
        if any(not path.is_file() or path.is_symlink() for path in sources):
            raise TranscriptionError("所选目录缺少模型文件：" + "、".join(required))
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".%s-import-" % key, dir=target.parent))
        try:
            manifest = {"repo_id": spec.repo_id, "revision": spec.revision, "files": {}}
            total, copied = sum(path.stat().st_size for path in sources), 0
            for source in sources:
                digest, size = hashlib.sha256(), 0
                with source.open("rb") as src, (staging / source.name).open("xb") as dest:
                    for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
                        _check_cancel(cancelled)
                        dest.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        copied += len(chunk)
                        _notify(progress, min(.99, copied / max(1, total)), "正在导入并校验模型")
                if size <= 0 or (source.name in expected and digest.hexdigest() != expected[source.name]):
                    raise TranscriptionError("导入模型版本或校验不匹配：%s；请使用所选模型的固定版本" % source.name)
                manifest["files"][source.name] = {"size": size, "sha256": digest.hexdigest()}
            _check_cancel(cancelled)
            (staging / _MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            staging.rename(target)
            _notify(progress, 1, "模型导入完成")
            return target
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _verify_model(key: str, cancelled: Cancelled) -> Path:
    manifest = _local_manifest(key)
    if manifest is None:
        raise TranscriptionError("请先到「设置 → 语音转文字」下载所选模型")
    directory = model_directory(key)
    for name, entry in manifest["files"].items():
        # 只读取目录中我们下载的预期文件，不接受清单里的任意路径。
        if name not in _filenames(_spec(key)):
            raise TranscriptionError("模型清单无效，请删除模型后重新下载")
        digest = hashlib.sha256()
        with (directory / name).open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
                _check_cancel(cancelled)
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise TranscriptionError("模型文件已损坏，请删除模型后重新下载：%s" % name)
    return directory


def _extract_audio(ffmpeg: str, source: Path, output: Path,
                   cancelled: Cancelled) -> float:
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(output)],
            stdout=subprocess.DEVNULL, stderr=errors, **paths.NO_WINDOW)
        try:
            while proc.poll() is None:
                _check_cancel(cancelled)
                time.sleep(0.1)
            _check_cancel(cancelled)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        if proc.returncode:
            errors.seek(0)
            detail = errors.read(4000).decode("utf-8", "replace").strip()
            raise TranscriptionError("无法提取视频音频，请确认视频包含有效音轨。\n%s" % detail)
    try:
        with wave.open(str(output), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise TranscriptionError("提取的音频无效") from exc
    if duration <= 0:
        raise TranscriptionError("视频没有可供识别的音频")
    return duration


def _load_model(directory: Path):
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as exc:
        raise TranscriptionError(
            "未安装或无法加载本地语音识别组件，请执行 pip install '.[speech]' 后重试") from exc
    return WhisperModel(str(directory), device="cpu", compute_type="int8",
                        cpu_threads=min(4, os.cpu_count() or 1), num_workers=1,
                        local_files_only=True)


def transcribe_isolated(source: Path, **kwargs) -> TranscriptionResult:
    from .speech_worker import run
    return run(source, **kwargs)


def _audio_windows(audio: Path, cancelled: Cancelled):
    """从磁盘逐块读单声道 PCM；不让长场次的波形/VAD 数组一次占满内存。"""
    with wave.open(str(audio), "rb") as wav:
        rate = wav.getframerate()
        if rate != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise TranscriptionError("提取的音频格式不正确")
        total = wav.getnframes()
        frames_per_window = int(_AUDIO_WINDOW_SECONDS * rate)
        if frames_per_window <= 0:
            raise TranscriptionError("音频分块时长无效")
        position = 0
        while position < total:
            _check_cancel(cancelled)
            expected = min(frames_per_window, total - position)
            pcm = wav.readframes(expected)
            if len(pcm) != expected * 2:
                raise TranscriptionError("提取的音频数据不完整")
            _check_cancel(cancelled)
            yield position / rate, expected / rate, pcm
            position += expected


def _audio_array(pcm: bytes):
    # NumPy 是 faster-whisper 的依赖，只在主动转写时导入。
    import numpy as np

    waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    waveform *= 1.0 / 32768.0
    return waveform


def _srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, rem = divmod(millis, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, millis)


def _write_srt(segments: tuple[Segment, ...], output: Path,
               overwrite: bool, cancelled: Cancelled) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(".%s.%s.part" % (output.name, uuid.uuid4().hex))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as fh:
            for index, segment in enumerate(segments, 1):
                _check_cancel(cancelled)
                fh.write("%d\n%s --> %s\n%s\n\n" % (
                    index, _srt_time(segment.start), _srt_time(segment.end), segment.text))
            fh.flush()
            os.fsync(fh.fileno())
        _check_cancel(cancelled)
        if overwrite:
            os.replace(temporary, output)
        else:
            # 同目录硬链接的发布是原子的；已有字幕即使在最后一刻出现也不会被覆盖。
            os.link(temporary, output)
    except FileExistsError as exc:
        raise TranscriptionError("语音字幕已存在，请选择其他输出位置：%s" % output.name) from exc
    finally:
        temporary.unlink(missing_ok=True)


def transcribe_video(source: Path, model: str = "small", language: Optional[str] = None,
                     *, ffmpeg: str = "", output: Optional[Path] = None,
                     progress: Progress = None, cancelled: Cancelled = None,
                     overwrite: bool = False) -> TranscriptionResult:
    """在工作线程中转写单个视频；取消/失败保留已有字幕，且永不改写视频。

    模型加载/单个推理片段内部无法强制中断；取消会在当前阶段返回后立即生效，
    在成功完成前不会发布 SRT。长录像的临时单声道音频只落盘，按不重叠的
    五分钟窗口送入同一个模型，窗口时间平移回当前视频；结束时自动清理。
    """
    _spec(model)
    source = Path(source)
    destination = Path(output) if output else source.with_name(source.stem + ".speech.srt")
    if not source.is_file():
        raise TranscriptionError("找不到待转写视频：%s" % source.name)
    if source.resolve() == destination.resolve():
        raise TranscriptionError("字幕输出不能覆盖源视频")
    if destination.exists() and not overwrite:
        raise TranscriptionError("语音字幕已存在，请选择其他输出位置：%s" % destination.name)
    if not model_dependency_available(model):
        raise TranscriptionError("请先安装本地语音识别组件：pip install '.[speech]'")
    _check_cancel(cancelled)
    try:
        with _model_lock(model):
            _notify(progress, 0.0, "正在校验本地模型")
            directory = _verify_model(model, cancelled)
            binary = video.find_ffmpeg(ffmpeg)
            with tempfile.TemporaryDirectory(prefix="lumina-speech-") as temporary:
                audio = Path(temporary) / "audio.wav"
                _notify(progress, 0.02, "正在提取音频")
                duration = _extract_audio(binary, source, audio, cancelled)
                _check_cancel(cancelled)
                _notify(progress, 0.05, "正在加载本地语音模型")
                if _spec(model).engine == "whisper":
                    engine = _load_model(directory)
                else:
                    from .china_asr import ChineseRecognizer
                    engine = ChineseRecognizer(directory, _spec(model).engine)
                _check_cancel(cancelled)
                _notify(progress, 0.08, "正在识别语音")
                segments = []
                selected_language = None if language in (None, "", "auto") else language
                detected_language = selected_language or "und"
                for window_start, window_duration, pcm in _audio_windows(audio, cancelled):
                    _check_cancel(cancelled)
                    waveform = _audio_array(pcm)
                    raw_segments, info = engine.transcribe(
                        waveform, language=selected_language, vad_filter=True,
                        beam_size=5, condition_on_previous_text=False)
                    window_has_speech = False
                    for raw in raw_segments:
                        _check_cancel(cancelled)
                        start, end = float(raw.start), float(raw.end)
                        if not math.isfinite(start) or not math.isfinite(end):
                            raise TranscriptionError("识别结果包含无效的字幕时间")
                        start, end = max(0.0, start), min(window_duration, end)
                        text = " ".join(str(raw.text).split()).strip()
                        if text and end > start:
                            segments.append(Segment(window_start + start,
                                                    window_start + end, text))
                            window_has_speech = True
                        fraction = min(1.0, (window_start + max(0.0, end)) / duration)
                        _notify(progress, 0.08 + 0.9 * fraction,
                                "正在识别语音（%.0f%%）" % (fraction * 100))
                    if window_has_speech and selected_language is None:
                        detected_language = str(info.language)
                        selected_language = detected_language
                    elif window_has_speech and detected_language == "und":
                        detected_language = str(info.language)
                    _check_cancel(cancelled)
                    completed = min(1.0, (window_start + window_duration) / duration)
                    _notify(progress, 0.08 + 0.9 * completed,
                            "正在识别语音（%.0f%%）" % (completed * 100))
                    # 每个窗口的惰性生成器与波形在下个窗口前释放。
                    del raw_segments, waveform, pcm
                _check_cancel(cancelled)
                if not segments:
                    _notify(progress, 1.0, "未识别到有效语音，未生成字幕")
                    return TranscriptionResult((), detected_language, duration, destination)
                result_segments = tuple(segments)
                _write_srt(result_segments, destination, overwrite, cancelled)
                _notify(progress, 1.0, "语音字幕已生成")
                return TranscriptionResult(result_segments, detected_language, duration,
                                           destination)
    except (TranscriptionError, TranscriptionCancelled):
        raise
    except Exception as exc:
        raise TranscriptionError("语音转文字失败：%s" % str(exc)[:500]) from exc
