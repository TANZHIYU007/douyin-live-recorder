"""录制完成后的持久化处理队列。

队列一次只处理一个任务，避免多房间同时收工时几个 ffmpeg 一起抢磁盘。任务在
应用数据目录落盘；崩溃或关机时处于 running 的任务，下次启动会回到 pending。
"""

from __future__ import annotations

import json
import logging
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
        self.processor = processor or self._process_subtitles
        self._tasks: list[ProcessingTask] = []
        self._stop = False
        self._condition = threading.Condition()
        self._thread: Optional[threading.Thread] = None
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
            if task is None or task.status != FAILED:
                return False
            task.status = PENDING
            task.message = "等待重试"
            task.error = ""
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
                log.exception("处理任务失败：%s", task.session_prefix)
                self._finish(task.id, FAILED, "处理失败", str(exc), list(task.results))
            else:
                message = "完成，生成 %d 个文件" % len(results) if results else "完成，没有生成新文件"
                self._finish(task.id, COMPLETED, message, "",
                             [str(path) for path in results])

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
            if task.status not in {PENDING, RUNNING, COMPLETED, FAILED}:
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
