"""本地录像库索引。

录像仍然以普通文件放在用户选择的目录里；这里不引入数据库，也不搬动文件，
只把同一场录制产生的视频、弹幕和字幕文件按前缀归成一组。这样旧版本录下的
内容也能直接出现在录像库中，换目录或手动整理后重新扫描即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .subtitle import VIDEO_SUFFIXES

SIDE_SUFFIXES = {".jsonl", ".xml", ".ass"}
KNOWN_SUFFIXES = set(VIDEO_SUFFIXES) | SIDE_SUFFIXES
_PART_SUFFIX = re.compile(r"(?:_r\d+)?_part\d+$|_r\d+$")
_STAMP = re.compile(r"^(\d{8})_(\d{6})_(.*)$", re.S)


@dataclass(frozen=True)
class RecordingSession:
    prefix: Path
    files: tuple[Path, ...]
    videos: tuple[Path, ...]
    jsonl: Optional[Path]
    xml: Optional[Path]
    recorded_at: float
    streamer: str
    title: str
    total_bytes: int

    @property
    def key(self) -> str:
        return str(self.prefix.resolve())

    @property
    def folder(self) -> Path:
        return self.prefix.parent


def recording_prefix(path: Path) -> Path:
    """从某个产出文件还原整场录制共用的前缀。"""
    stem = path.stem
    if stem.endswith("_弹幕版"):
        stem = stem[:-4]
    stem = _PART_SUFFIX.sub("", stem)
    return path.parent / stem


def _recorded_at(prefix: Path, files: Iterable[Path]) -> float:
    match = _STAMP.match(prefix.name)
    if match:
        try:
            return datetime.strptime(match.group(1) + match.group(2),
                                     "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    timestamps = []
    for path in files:
        try:
            timestamps.append(path.stat().st_mtime)
        except OSError:
            pass
    return min(timestamps, default=0.0)


def _title(prefix: Path) -> str:
    match = _STAMP.match(prefix.name)
    return (match.group(3) if match else prefix.name) or "（无标题）"


def scan_library(root: Path) -> list[RecordingSession]:
    """递归扫描录像目录；坏文件或瞬间被移动的文件只跳过，不中断整个索引。"""
    root = Path(root)
    if not root.is_dir():
        return []

    grouped: dict[Path, list[Path]] = {}
    try:
        candidates = root.rglob("*")
        for path in candidates:
            try:
                if not path.is_file() or path.suffix.lower() not in KNOWN_SUFFIXES:
                    continue
            except OSError:
                continue
            grouped.setdefault(recording_prefix(path), []).append(path)
    except OSError:
        return []

    sessions: list[RecordingSession] = []
    for prefix, members in grouped.items():
        files = tuple(sorted(members, key=lambda p: p.name))
        videos = tuple(path for path in files if path.suffix.lower() in VIDEO_SUFFIXES)
        jsonl = next((path for path in files if path.suffix.lower() == ".jsonl"), None)
        xml = next((path for path in files if path.suffix.lower() == ".xml"), None)
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        sessions.append(RecordingSession(
            prefix=prefix,
            files=files,
            videos=videos,
            jsonl=jsonl,
            xml=xml,
            recorded_at=_recorded_at(prefix, files),
            streamer=prefix.parent.name,
            title=_title(prefix),
            total_bytes=size,
        ))
    return sorted(sessions, key=lambda item: (item.recorded_at, item.prefix.name),
                  reverse=True)
