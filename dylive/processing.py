"""录制完成后的持久化处理队列。

队列一次只处理一个任务，避免多房间同时收工时几个 ffmpeg 一起抢磁盘。任务在
应用数据目录落盘；崩溃或关机时处于 running 的任务，下次启动会回到 pending。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import paths, subtitle, video

log = logging.getLogger("processing")

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE = {PENDING, RUNNING}


def _resolved(path: Path | str) -> str:
    """返回稳定的绝对路径；normcase 只用于比较，不改变落盘展示。"""
    return str(Path(path).resolve())


def _path_key(path: Path | str) -> str:
    return os.path.normcase(_resolved(path))


@dataclass
class ProcessingTask:
    id: str
    session_prefix: str
    jsonl: str
    videos: list[str]
    offsets: list[float] = field(default_factory=list)
    style: dict = field(default_factory=dict)
    kinds: list[str] = field(default_factory=lambda: list(subtitle.DEFAULT_KINDS))
    replace: bool = True
    status: str = PENDING
    message: str = "等待处理"
    error: str = ""
    results: list[str] = field(default_factory=list)
    processed_videos: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    task_type: str = "subtitles"
    speech: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "ProcessingTask":
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in raw.items() if key in known})


def queue_path() -> Path:
    return paths.app_data_dir() / "processing-queue.json"


class PostProcessQueue:
    """线程安全的串行队列；界面通过 snapshot() 轮询，不跨线程碰 Qt。"""

    def __init__(self, state_path: Optional[Path] = None,
                 ffmpeg_provider: Optional[Callable[[], str]] = None,
                 processor: Optional[Callable] = None,
                 autostart: bool = True):
        self.state_path = Path(state_path) if state_path else queue_path()
        self.ffmpeg_provider = ffmpeg_provider or video.find_ffmpeg
        self.processor = processor or self._process_task
        self._tasks: list[ProcessingTask] = []
        self._stop = False
        self._condition = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._cancel_events: dict[str, threading.Event] = {}
        self._load()
        if autostart:
            self.start()

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._work, name="post-process",
                                            daemon=True)
            self._thread.start()

    def enqueue_subtitles(self, session_prefix: Path, jsonl: Path,
                          videos: Sequence[Path], offsets: Sequence[float] = (),
                          style: Optional[subtitle.Style] = None,
                          kinds: Sequence[str] = subtitle.DEFAULT_KINDS,
                          replace: bool = True) -> ProcessingTask:
        jsonl = Path(jsonl).resolve()
        video_paths = [_resolved(path) for path in videos]
        if not jsonl.is_file():
            raise ValueError("找不到弹幕文件：%s" % jsonl)
        if not video_paths:
            raise ValueError("这场录像没有可处理的视频")

        with self._condition:
            for task in self._tasks:
                if _path_key(task.jsonl) == _path_key(jsonl) and task.status in ACTIVE:
                    return ProcessingTask.from_dict(asdict(task))
            task = ProcessingTask(
                id=uuid.uuid4().hex,
                session_prefix=_resolved(session_prefix),
                jsonl=str(jsonl),
                videos=video_paths,
                offsets=[float(value) for value in offsets],
                style=asdict(style or subtitle.Style()),
                kinds=list(kinds),
                replace=bool(replace),
            )
            self._tasks.append(task)
            self._trim_locked()
            self._save_locked()
            self._condition.notify_all()
            return ProcessingTask.from_dict(asdict(task))

    def enqueue_transcription(self, session_prefix: Path, videos: Sequence[Path],
                              offsets: Sequence[float] = (), jsonl: Optional[Path] = None,
                              model: str = "small", language: str = "auto",
                              subtitle_mode: str = "combined",
                              style: Optional[subtitle.Style] = None) -> ProcessingTask:
        """只为用户选中的场次加入本地转写任务，始终保留输入视频。"""
        from . import transcription
        from .replay import part_key

        sources = list(dict.fromkeys(_resolved(path) for path in videos))
        if not sources or any(not Path(path).is_file() for path in sources):
            raise ValueError("请选择一场已有视频的录像，文件可能已移动或仍在处理。")
        if not transcription.model_dependency_available(model):
            raise ValueError("尚未安装语音识别组件，请安装本项目的 speech 可选依赖。")
        if not transcription.model_status(model):
            raise ValueError("模型尚未下载完成，请先到设置中的语音转文字下载模型。")
        if subtitle_mode not in {"combined", "voice"}:
            raise ValueError("未知的语音字幕显示方式")
        if language not in {"auto", "zh", "en", "ja", "ko"}:
            raise ValueError("请选择支持的语音语言或自动识别。")
        starts = [float(value) for value in offsets]
        if starts and (len(starts) != len(sources) or any(
                not math.isfinite(value) or value < 0 for value in starts)):
            raise ValueError("视频分片时间记录无效，请刷新录像库后重试。")
        prefix = _resolved(session_prefix)
        with self._condition:
            if any(_path_key(task.session_prefix) == _path_key(prefix)
                   and task.status in ACTIVE for task in self._tasks):
                raise ValueError("这场录像已有等待或运行中的处理任务，请完成后再转写。")
            task_id = uuid.uuid4().hex
            reserved = {str(path) for task in self._tasks if task.status in ACTIVE
                        for path in task.speech.get("output_files", {}).values()}
            outputs, srts, signatures = {}, {}, {}
            for name in sources:
                source = Path(name)
                base = source.with_name(part_key(source) + "_语音字幕")
                dest, index = base.with_name(base.name + ".mkv"), 2
                while (dest.exists() or str(dest) in reserved
                       or dest.with_name(dest.stem + "_弹幕.ass").exists()):
                    dest = base.with_name(base.name + "_%d.mkv" % index)
                    index += 1
                reserved.add(str(dest))
                outputs[name] = str(dest)
                srts[name] = str(source.with_name(
                    source.stem + "_语音识别-" + task_id[:8] + ".srt"))
                stat = source.stat()
                signatures[name] = [stat.st_size, stat.st_mtime_ns]
            task = ProcessingTask(
                id=task_id, session_prefix=prefix,
                jsonl=_resolved(jsonl) if jsonl else "", videos=sources, offsets=starts,
                style=asdict(style or subtitle.Style()), replace=False,
                task_type="transcription", message="等待语音转写",
                speech={"model": model, "language": language,
                        "subtitle_mode": subtitle_mode, "srt_files": srts,
                        "output_files": outputs, "source_signatures": signatures,
                        "srt_ready": [], "empty_videos": []},
            )
            self._tasks.append(task)
            self._trim_locked()
            self._save_locked()
            self._condition.notify_all()
            return ProcessingTask.from_dict(asdict(task))

    def cancel(self, task_id: str) -> bool:
        with self._condition:
            task = next((item for item in self._tasks if item.id == task_id), None)
            if task is None or task.status not in ACTIVE:
                return False
            if task.status == RUNNING and task.task_type != "transcription":
                return False
            self._cancel_events.setdefault(task.id, threading.Event()).set()
            if task.status == PENDING:
                task.status = CANCELLED
                task.message = "已取消，可重试继续"
            else:
                task.message = "正在取消，等待当前识别步骤安全结束…"
            task.updated_at = time.time()
            self._save_locked()
            self._condition.notify_all()
            return True

    def snapshot(self) -> list[ProcessingTask]:
        with self._condition:
            return [ProcessingTask.from_dict(asdict(task)) for task in self._tasks]

    def latest_for(self, session_prefix: Path) -> Optional[ProcessingTask]:
        key = _path_key(session_prefix)
        with self._condition:
            task = next((item for item in reversed(self._tasks)
                         if _path_key(item.session_prefix) == key), None)
            return ProcessingTask.from_dict(asdict(task)) if task else None

    def retry(self, task_id: str) -> bool:
        with self._condition:
            task = next((item for item in self._tasks if item.id == task_id), None)
            if task is None or task.status not in {FAILED, CANCELLED}:
                return False
            task.status = PENDING
            task.message = "等待重试"
            task.error = ""
            self._cancel_events.pop(task.id, None)
            task.updated_at = time.time()
            self._save_locked()
            self._condition.notify_all()
            return True

    def clear_finished(self) -> int:
        with self._condition:
            before = len(self._tasks)
            # 失败项要继续留在界面里，用户才能查看原因或点击重试。
            self._tasks = [task for task in self._tasks
                           if task.status != COMPLETED]
            removed = before - len(self._tasks)
            if removed:
                self._save_locked()
            return removed

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._condition:
            self._stop = True
            for task in self._tasks:
                if task.status == RUNNING and task.task_type == "transcription":
                    self._cancel_events.setdefault(task.id, threading.Event()).set()
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        with self._condition:
            while any(task.status in ACTIVE for task in self._tasks):
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._condition.wait(min(0.2, remaining))
            return True

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._stop:
                    task = next((item for item in self._tasks
                                 if item.status == PENDING), None)
                    if task is not None:
                        break
                    self._condition.wait()
                if self._stop:
                    return
                task.status = RUNNING
                task.message = "正在准备…"
                task.error = ""
                task.attempts += 1
                task.updated_at = time.time()
                self._save_locked()

            try:
                task_id = task.id
                results = self.processor(
                    task, lambda message, key=task_id: self._progress(key, message))
            except Exception as exc:  # noqa: BLE001 - 失败要留在队列里供用户重试
                event = self._cancel_events.get(task.id)
                if task.task_type == "transcription" and event and event.is_set():
                    status = PENDING if self._stop else CANCELLED
                    message = "上次未完成，等待继续" if self._stop else "已取消，可重试继续"
                    self._finish(task.id, status, message, "", list(task.results))
                else:
                    log.exception("处理任务失败：%s", task.session_prefix)
                    self._finish(task.id, FAILED, "处理失败", str(exc), list(task.results))
            else:
                message = "完成，生成 %d 个文件" % len(results) if results else "完成，没有生成新文件"
                if task.task_type == "transcription" and task.speech.get("empty_videos"):
                    message += "（%d 个片段未识别到语音）" % len(task.speech["empty_videos"])
                self._finish(task.id, COMPLETED, message, "",
                             [str(path) for path in results])

    def _process_task(self, task: ProcessingTask,
                      progress: Callable[[str], None]) -> list[Path]:
        if task.task_type == "transcription":
            return self._process_transcription(task, progress)
        if task.task_type != "subtitles":
            raise ValueError("未知的处理任务类型：%s" % task.task_type)
        return self._process_subtitles(task, progress)

    def _process_transcription(self, task: ProcessingTask,
                               progress: Callable[[str], None]) -> list[Path]:
        from . import speech_subtitle, transcription

        event = self._cancel_events.setdefault(task.id, threading.Event())
        cancelled = lambda: event.is_set() or self._stop
        ffmpeg = self.ffmpeg_provider()
        if not task.offsets:
            starts, elapsed = [], 0.0
            for source in task.videos:
                starts.append(elapsed)
                elapsed += subtitle.probe(ffmpeg, Path(source)).get("duration", 0.0)
            with self._condition:
                task.offsets = starts
                self._save_locked()
        for index, name in enumerate(task.videos):
            if name in task.processed_videos:
                continue
            if cancelled():
                raise transcription.TranscriptionCancelled("已取消语音转写")
            source = Path(name)
            stat = source.stat()
            expected = task.speech["source_signatures"][name]
            if [stat.st_size, stat.st_mtime_ns] != expected:
                raise ValueError("视频在加入队列后已变化，请刷新录像库并重新添加任务：%s" % source.name)
            srt = Path(task.speech["srt_files"][name])
            label = "片段 %d/%d" % (index + 1, len(task.videos))
            if name not in task.speech["srt_ready"] or not srt.is_file():
                # 写字幕后、队列存档前断电时，保留未确认归属的文件并换名重做。
                if srt.exists():
                    stem, number = srt.stem, 2
                    while srt.exists():
                        srt = srt.with_name(stem + "-%d.srt" % number)
                        number += 1
                    with self._condition:
                        task.speech["srt_files"][name] = str(srt)
                        self._save_locked()
                result = transcription.transcribe_isolated(
                    source, model=task.speech["model"],
                    language=task.speech["language"], ffmpeg=ffmpeg, output=srt,
                    progress=lambda value, text, label=label: progress(
                        "%s · %.0f%% · %s" % (label, value * 100, text)),
                    cancelled=cancelled,
                )
                with self._condition:
                    if not result.segments:
                        task.speech["empty_videos"].append(name)
                        task.processed_videos.append(name)
                    else:
                        task.speech["srt_ready"].append(name)
                        task.speech.setdefault("detected_languages", {})[name] = getattr(
                            result, "language", task.speech["language"])
                        if str(srt) not in task.results:
                            task.results.append(str(srt))
                    self._save_locked()
                if not result.segments:
                    continue
            if cancelled():
                raise transcription.TranscriptionCancelled("已取消语音转写，SRT 已保存")
            stat = source.stat()
            if [stat.st_size, stat.st_mtime_ns] != expected:
                raise ValueError("转写期间视频已变化，请重新添加任务：%s" % source.name)
            destination = Path(task.speech["output_files"][name])
            if destination.exists() or destination.with_name(destination.stem + "_弹幕.ass").exists():
                from .replay import part_key
                base = source.with_name(part_key(source) + "_语音字幕")
                number = 2
                while (destination.exists()
                       or destination.with_name(destination.stem + "_弹幕.ass").exists()):
                    destination = base.with_name(base.name + "_%d.mkv" % number)
                    number += 1
                with self._condition:
                    task.speech["output_files"][name] = str(destination)
                    self._save_locked()
            output = speech_subtitle.mux_speech(
                ffmpeg, source, srt,
                jsonl=Path(task.jsonl) if task.jsonl else None,
                offset=task.offsets[index], style=subtitle.Style(**task.style),
                out=destination,
                combined=task.speech["subtitle_mode"] == "combined",
                language=task.speech.get("detected_languages", {}).get(name, "und"),
                progress=progress, cancelled=cancelled,
            )
            with self._condition:
                task.processed_videos.append(name)
                task.results.append(str(output))
                chat_file = output.with_name(output.stem + "_弹幕.ass")
                if chat_file.is_file():
                    task.results.append(str(chat_file))
                task.updated_at = time.time()
                self._save_locked()
        return [Path(path) for path in task.results]

    def _process_subtitles(self, task: ProcessingTask,
                           progress: Callable[[str], None]) -> list[Path]:
        remaining = [i for i, path in enumerate(task.videos)
                     if path not in task.processed_videos]
        if not remaining:
            return [Path(path) for path in task.results]
        for index in remaining:
            if not Path(task.videos[index]).is_file():
                raise subtitle.SubtitleProcessError("找不到视频文件：%s" % task.videos[index])
        ffmpeg = self.ffmpeg_provider()
        if not task.offsets:
            # 必须在替换任何原文件之前保存整场时间轴，重试时不能从剩余片段重算。
            offsets = []
            elapsed = 0.0
            for path in task.videos:
                offsets.append(elapsed)
                elapsed += subtitle.probe(ffmpeg, Path(path)).get("duration", 0.0)
            with self._condition:
                task.offsets = offsets
                self._save_locked()

        def processed(source: Path, result: Optional[Path]) -> None:
            with self._condition:
                task.processed_videos.append(str(source))
                if result is not None:
                    task.results.append(str(result))
                task.updated_at = time.time()
                self._save_locked()

        subtitle.process(
            ffmpeg,
            Path(task.jsonl),
            [Path(task.videos[i]) for i in remaining],
            offsets=[task.offsets[i] if i < len(task.offsets) else 0.0 for i in remaining],
            style=subtitle.Style(**task.style),
            kinds=task.kinds,
            replace=task.replace,
            progress=progress,
            on_processed=processed,
        )
        return [Path(path) for path in task.results]

    def _progress(self, task_id: str, message: str) -> None:
        with self._condition:
            task = next((item for item in self._tasks if item.id == task_id), None)
            if task is None:
                return
            task.message = str(message)
            task.updated_at = time.time()
            self._save_locked()

    def _finish(self, task_id: str, status: str, message: str,
                error: str, results: list[str]) -> None:
        with self._condition:
            task = next((item for item in self._tasks if item.id == task_id), None)
            if task is None:
                return
            task.status = status
            task.message = message
            task.error = error
            task.results = results
            task.updated_at = time.time()
            self._save_locked()
            self._condition.notify_all()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                task = ProcessingTask.from_dict(item)
            except (TypeError, ValueError):
                continue
            if task.status == RUNNING:
                task.status = PENDING
                task.message = "上次未完成，等待继续"
            if task.status not in {PENDING, RUNNING, COMPLETED, FAILED, CANCELLED}:
                continue
            if task.task_type not in {"subtitles", "transcription"} or not isinstance(task.speech, dict):
                continue
            self._tasks.append(task)

    def _trim_locked(self) -> None:
        if len(self._tasks) <= 200:
            return
        active = [task for task in self._tasks if task.status in ACTIVE]
        finished = [task for task in self._tasks if task.status not in ACTIVE]
        self._tasks = finished[-max(0, 200 - len(active)):] + active

    def _save_locked(self) -> None:
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps([asdict(task) for task in self._tasks],
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.state_path)
        except OSError as exc:
            log.warning("保存处理队列失败：%s", exc)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
