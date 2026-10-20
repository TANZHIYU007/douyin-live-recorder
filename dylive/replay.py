"""本地弹幕搜索和回放时间轴（不依赖 Qt，不修改原视频与弹幕）。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import subtitle, video
from .library import RecordingSession, recording_prefix
from .profiles import clean_recording
from .utils import files_with_prefix

MAX_EVENTS = 200_000
MAX_RESULTS = 500
MAX_LINE = 1024 * 1024
MAX_TIME = 366 * 24 * 3600  # 防止损坏记录中的极大数字溢出 Qt 的毫秒时间。


@dataclass(frozen=True)
class ReplayEvent:
    offset: float
    user: str
    content: str
    kind: str

    def matches(self, query: str) -> bool:
        return query in (self.user + " " + self.content).casefold()


@dataclass
class ReadStats:
    skipped: int = 0
    truncated: bool = False


def iter_events(path: Path, cancel: threading.Event | None = None,
                stats: ReadStats | None = None) -> Iterator[ReplayEvent]:
    stats = stats if stats is not None else ReadStats()
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        while not (cancel and cancel.is_set()):
            line = fh.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith("\n"):
                    if cancel and cancel.is_set():
                        return
                    line = fh.readline(MAX_LINE + 1)
                stats.skipped += 1
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError()
                offset = raw.get("offset")
                if (type(offset) not in (int, float) or not math.isfinite(offset)
                        or not 0 <= offset <= MAX_TIME):
                    raise ValueError()
                user, content, kind = (raw.get(k, "") for k in ("user_name", "content", "kind"))
                if not all(isinstance(v, str) for v in (user, content, kind)):
                    raise ValueError()
                if user or content:
                    yield ReplayEvent(float(offset), user, content, kind)
            except (ValueError, OverflowError):
                stats.skipped += 1


def load_events(path: Path | None, cancel: threading.Event | None = None,
                limit: int = MAX_EVENTS, *, focus_offset: float | None = None
                ) -> tuple[list[ReplayEvent], ReadStats]:
    stats = ReadStats()
    events = []
    if path and focus_offset is not None:
        # 大场次从跨场搜索跳入时，保留命中前后的窗口，不必把几十万条全部塞入内存。
        before = deque(maxlen=max(1, limit // 2))
        for event in iter_events(path, cancel, stats):
            if event.offset < focus_offset:
                if len(before) == before.maxlen:
                    stats.truncated = True
                before.append(event)
            elif len(events) < limit - before.maxlen:
                events.append(event)
            else:
                stats.truncated = True
        events.extend(before)
    elif path:
        for event in iter_events(path, cancel, stats):
            if len(events) >= limit:
                stats.truncated = True
                break
            events.append(event)
    events.sort(key=lambda event: event.offset)
    return events, stats


def search_sessions(sessions: list[RecordingSession], query: str,
                    cancel: threading.Event, limit: int = MAX_RESULTS) -> tuple[list, str]:
    """流式跨场搜索；上限只限制显示，不影响单场回放。"""
    query = query.strip().casefold()
    hits, failed = [], 0
    stats = ReadStats()
    if not query:
        return hits, "请输入弹幕内容或用户名。"
    for session in sessions:
        if cancel.is_set():
            return hits, "已取消搜索"
        if session.jsonl is None:
            continue
        try:
            for event in iter_events(session.jsonl, cancel, stats):
                if event.matches(query):
                    if len(hits) >= limit:
                        return hits, "仅显示前 %d 条，请缩小关键词范围。" % limit
                    hits.append((session, event))
        except OSError:
            failed += 1
    note = "找到 %d 条 · 双击跳转回放" % len(hits)
    if failed or stats.skipped:
        note += "（跳过 %d 个不可读文件、%d 条无效记录）" % (failed, stats.skipped)
    return hits, note


def metadata_path(prefix: Path) -> Path:
    # 不用 with_suffix：标题中的点号也是前缀的一部分。
    return prefix.with_name(prefix.name + ".session.json")


def save_metadata(prefix: Path, room_id: str, parts: list[dict], settings: dict) -> None:
    target = metadata_path(prefix)
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "room_id": room_id, "parts": parts,
                       "recording": clean_recording(settings)}, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def read_metadata(prefix: Path) -> dict:
    try:
        with metadata_path(prefix).open("r", encoding="utf-8") as fh:
            raw = json.loads(fh.read(4 * 1024 * 1024))
        return raw if isinstance(raw, dict) and raw.get("version") == 1 else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def part_key(path: Path) -> str:
    stem = path.stem
    return stem[:-4] if stem.endswith("_弹幕版") else stem


def _saved_parts(metadata: dict, task=None) -> dict[str, tuple[float, float]]:
    parts = metadata.get("parts", [])
    known = {}
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("file"), str):
                start = _number(part.get("start"), -1)
                if start >= 0:
                    known[part_key(Path(part["file"]))] = (
                        start, max(0, _number(part.get("duration"))))
    if task:
        for name, start in zip(task.videos, task.offsets, strict=False):
            value = _number(start, -1)
            if value >= 0:
                known.setdefault(part_key(Path(name)), (value, 0.0))
    return known


def saved_offsets(prefix: Path, videos: list[Path], task=None) -> list[float]:
    """历史录像补做字幕也应复用原时间轴；缺任何一段就交给原有推算逻辑。"""
    known = _saved_parts(read_metadata(prefix), task)
    return [known[part_key(p)][0] for p in videos] if all(part_key(p) in known for p in videos) else []


def current_videos(session: RecordingSession) -> list[Path]:
    """同时有原片和字幕副本时只回放一份，字幕替换后仍认得原分片。"""
    candidates = {p for p in files_with_prefix(session.prefix)
                  if p.suffix.lower() in subtitle.VIDEO_SUFFIXES} | set(session.videos)
    selected: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda p: (p.stem.endswith("_弹幕版"), p.name)):
        if recording_prefix(path) == session.prefix and path.is_file():
            selected.setdefault(part_key(path), path)
    return sorted(selected.values(), key=lambda p: [int(v) if v.isdigit() else v
                  for v in re.split(r"(\d+)", part_key(p))])


def _number(value: object, default: float = 0.0) -> float:
    return (float(value) if type(value) in (int, float) and math.isfinite(value)
            and abs(value) <= MAX_TIME else default)


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    duration: float


@dataclass
class Timeline:
    segments: list[Segment] = field(default_factory=list)
    delay: float = 0.0
    note: str = ""

    def locate(self, event_offset: float) -> tuple[int, float] | None:
        moment = event_offset + self.delay
        for index, part in enumerate(self.segments):
            if part.start >= 0 and part.start <= moment < part.start + part.duration:
                return index, moment - part.start
        return None


def build_timeline(session: RecordingSession, task=None, *, ffmpeg: str = "",
                   cancel: threading.Event | None = None) -> Timeline:
    meta = read_metadata(session.prefix)
    recording = clean_recording(meta.get("recording"))
    delay = recording.get("subtitle_delay", _number((getattr(task, "style", {}) or {}).get("delay")))
    known = _saved_parts(meta, task)
    if not ffmpeg:
        try:
            ffmpeg = video.find_ffmpeg()
        except RuntimeError:
            pass
    timeline = Timeline(delay=delay)
    estimated = False
    end = 0.0
    for path in current_videos(session):
        if cancel and cancel.is_set():
            break
        timing = known.get(part_key(path))
        start, duration = timing if timing else (end if end is not None else -1, 0.0)
        estimated |= timing is None
        if not duration and ffmpeg:
            duration = max(0, _number(subtitle.probe(ffmpeg, path).get("duration")))
        timeline.segments.append(Segment(path, start, duration))
        end = start + duration if start >= 0 and duration > 0 else None
    notes = []
    if estimated:
        notes.append("历史录像缺少时间记录，按分片时长推算；起录与断线处可能有偏差")
    if any(not part.duration or part.start < 0 for part in timeline.segments):
        notes.append("部分分片时长不可读，无法定位其中的弹幕；请检查文件和 FFprobe")
    if not timeline.segments:
        notes.append("没有可用视频，可搜索弹幕记录")
    timeline.note = "；".join(notes)
    return timeline
