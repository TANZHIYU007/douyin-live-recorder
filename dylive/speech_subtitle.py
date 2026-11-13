"""把语音字幕和弹幕合成可同时显示的字幕轨，始终保留原片。

成品分别提供「语音字幕」和「弹幕字幕」轨，还可提供一次显示两者的合并轨。
语音位于底部中央，弹幕在其上方的独立区域，支持双字幕的播放器也不会重叠。
音视频及原有字幕轨全部保留，不重新编码画面。
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from . import subtitle
from .paths import NO_WINDOW, subtitle_font

SPEECH_TITLE = "语音字幕"
DANMAKU_TITLE = "弹幕字幕"
COMBINED_TITLE = "语音字幕 + 弹幕"


class SpeechSubtitleCancelled(subtitle.SubtitleProcessError):
    """用户取消封装；原片及既有输出不变。"""


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise SpeechSubtitleCancelled("已取消语音字幕封装")


def _run(command: list[str], cancelled=None, timeout: float = 3600) -> bytes:
    """stderr 放临时文件，避免长任务堵住管道；取消会终止且回收子进程。"""
    _check_cancelled(cancelled)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, **NO_WINDOW)
        except OSError as exc:
            raise subtitle.SubtitleProcessError("无法启动媒体工具：%s" % exc) from exc
        started = time.monotonic()
        try:
            while process.poll() is None:
                _check_cancelled(cancelled)
                if time.monotonic() - started > timeout:
                    raise subtitle.SubtitleProcessError("媒体工具处理超时")
                time.sleep(0.1)
            if process.returncode:
                stderr.seek(0, os.SEEK_END)
                stderr.seek(max(0, stderr.tell() - 1000))
                detail = stderr.read().decode("utf-8", "replace").strip()
                raise subtitle.SubtitleProcessError(
                    "媒体工具处理失败（退出码 %d）：%s" % (process.returncode, detail))
            stdout.seek(0)
            return stdout.read()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _probe(ffmpeg: str, path: Path, cancelled=None) -> dict:
    output = _run([subtitle.find_ffprobe(ffmpeg), "-v", "error", "-show_streams",
                   "-show_format", "-of", "json", str(path)], cancelled, timeout=60)
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or not result.get("streams"):
            raise ValueError("没有媒体轨道")
        return result
    except (ValueError, TypeError) as exc:
        raise subtitle.SubtitleProcessError("无法读取视频信息：%s" % path.name) from exc


_TIMING = re.compile(
    r"^(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})\s*-->\s*"
    r"(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})(?:\s+.*)?$")


def _read_cues(srt: Path, duration: float) -> list[tuple[float, float, str]]:
    try:
        text = srt.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        raise subtitle.SubtitleProcessError("无法读取语音字幕：%s" % exc) from exc
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        index = 1 if lines[0].strip().isdigit() else 0
        match = _TIMING.fullmatch(lines[index].strip()) if len(lines) > index else None
        if match is None:
            raise subtitle.SubtitleProcessError("语音字幕时间格式不正确：%s" % srt.name)
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        content = "\n".join(lines[index + 1:]).strip()
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise subtitle.SubtitleProcessError("语音字幕时间范围不正确：%s" % srt.name)
        if content and start < duration:
            cues.append((start, min(end, duration), content))
    if not cues:
        raise subtitle.SubtitleProcessError("没有可加入视频的语音字幕")
    return sorted(cues)


def _write_srt(cues: list[tuple[float, float, str]], out: Path) -> None:
    def stamp(value: float) -> str:
        milliseconds = round(value * 1000)
        seconds, ms = divmod(milliseconds, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, ms)

    out.write_text("\n\n".join("%d\n%s --> %s\n%s" %
        (index, stamp(start), stamp(end), text)
        for index, (start, end, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")


def _speech_lines(cues: list[tuple[float, float, str]], width: int,
                  size: int) -> list[str]:
    """长语句分成两行一屏，按字符比例拆分时段，不丢弃转写文本。"""
    result = []
    for start, end, text in cues:
        wrapped, line = [], ""
        for char in text.replace("\r", ""):
            if char == "\n":
                if line:
                    wrapped.append(line)
                    line = ""
                continue
            if line and subtitle._text_width(line + char, size) > width * 0.88:
                wrapped.append(line)
                line = ""
            line += char
        if line:
            wrapped.append(line)
        groups = [wrapped[i:i + 2] for i in range(0, len(wrapped), 2)]
        total = sum(len(part) for group in groups for part in group)
        used = 0
        for group in groups:
            count = sum(len(part) for part in group)
            a = start + (end - start) * used / max(1, total)
            used += count
            b = start + (end - start) * used / max(1, total)
            safe = "\\N".join(subtitle._escape(part) for part in group)
            result.append("Dialogue: 2,%s,%s,Speech,,0,0,0,,%s\n" % (
                subtitle._ass_time(a), subtitle._ass_time(b), safe))
    return result


def _trim_ass(text: str, duration: float) -> str:
    """滚动弹幕的离场时间不能把短片容器延长到画面结束之后。"""
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            times = []
            for item in parts[1:3]:
                hour, minute, second = item.split(":")
                times.append(int(hour) * 3600 + int(minute) * 60 + float(second))
            start, end = times
            if start >= duration:
                continue
            parts[2] = subtitle._ass_time(min(end, duration))
            line = ",".join(parts)
        lines.append(line)
    return "".join(lines)


def build_combined_ass(srt: Path, out: Path, *, width: int, height: int,
                       duration: float, jsonl: Optional[Path] = None,
                       offset: float = 0.0, style: Optional[subtitle.Style] = None,
                       danmaku_out: Optional[Path] = None) -> int:
    """语音时间相对本视频；弹幕按整场时间窗口筛选后平移，保留片首聊天。"""
    if not math.isfinite(offset) or offset < 0 or not math.isfinite(duration) or duration <= 0:
        raise subtitle.SubtitleProcessError("视频分片时间信息不正确")
    if width <= 0 or height <= 0:
        raise subtitle.SubtitleProcessError("无法取得视频分辨率")
    size = max(14, round(min(width, height) * 0.042))
    speech = _speech_lines(_read_cues(srt, duration), width, size)
    if len(speech) >= subtitle.MAX_DIALOGUE - 4:
        raise subtitle.SubtitleProcessError("语音字幕条目过多，请分段处理录像")
    reserve = min(0.45, max(0.18, (size * 3.0) / height + 0.04))
    st = replace(style or subtitle.Style(mode=subtitle.CHAT), width=width, height=height)
    # 气泡新消息有向上滑入动画，额外留出一行，避免动画期间盖住语音。
    st.margin_y = max(st.margin_y, reserve + st.size * 1.45 / height)
    st.reserve = max(st.reserve, reserve)
    st.max_rate = min(st.max_rate, max(0.1,
        (subtitle.MAX_DIALOGUE - len(speech) - 4) / (2 * duration)))
    events = subtitle.load_events(jsonl, strict=True) if jsonl is not None else []
    subtitle.build_ass(events, out, st, shift=offset, window=(offset, offset + duration))
    text = _trim_ass(out.read_text(encoding="utf-8-sig"), duration)
    if danmaku_out is not None:
        # 与合并轨使用完全相同的位置；分离显示两条轨道也不会盖住语音。
        danmaku_out.write_text(text, encoding="utf-8-sig")
    margin = max(4, round(height * 0.025))
    speech_style = (
        "Style: Speech,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,"
        "0,0,0,0,100,100,0,0,1,2,1,2,%d,%d,%d,1\n\n" %
        (subtitle_font(), size, round(width * 0.05), round(width * 0.05), margin))
    text = text.replace("[Events]\n", speech_style + "[Events]\n", 1)
    if text.count("Dialogue:") + len(speech) > subtitle.MAX_DIALOGUE:
        raise subtitle.SubtitleProcessError("合并字幕条目过多，请分段处理录像")
    out.write_text(text + "".join(speech), encoding="utf-8-sig")
    return len(speech)


def _output_path(video: Path) -> Path:
    prefix = video.stem + "_语音字幕"
    candidate = video.with_name(prefix + ".mkv")
    number = 2
    while candidate.exists() or candidate.with_name(candidate.stem + "_弹幕.ass").exists():
        candidate = video.with_name(prefix + "_%d.mkv" % number)
        number += 1
    return candidate


def _validate(source: dict, result: dict, added: int) -> None:
    before, after = source["streams"], result["streams"]
    expected = Counter(stream.get("codec_type") for stream in before)
    expected["subtitle"] += added
    if Counter(stream.get("codec_type") for stream in after) != expected:
        raise subtitle.SubtitleProcessError("封装成品缺少原有轨道，原片已保留")
    for kind in ("video", "audio"):
        original = [s.get("codec_name") for s in before if s.get("codec_type") == kind]
        actual = [s.get("codec_name") for s in after if s.get("codec_type") == kind]
        if original != actual:
            raise subtitle.SubtitleProcessError("封装成品音视频轨道异常，原片已保留")
    try:
        a = float(source["format"]["duration"])
        b = float(result["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise subtitle.SubtitleProcessError("无法验证封装成品时长，原片已保留") from exc
    if not math.isfinite(b) or abs(a - b) > 2.0:
        raise subtitle.SubtitleProcessError("封装成品时长异常，原片已保留")


def mux_speech(ffmpeg: str, video: Path, srt: Path, *, jsonl: Optional[Path] = None,
               offset: float = 0.0, style: Optional[subtitle.Style] = None,
               out: Optional[Path] = None, combined: bool = True,
               language: str = "und", progress: Optional[Callable[[str], None]] = None,
               cancelled: Optional[Callable[[], bool]] = None) -> Path:
    """添加分离的语音/弹幕轨和可选合并轨；原片和既有输出绝不覆盖。

    combined=False 仍会在存在 JSONL 时添加分离的安全布局弹幕轨，默认显示语音。
    """
    video, srt = Path(video), Path(srt)
    if not video.is_file() or not srt.is_file():
        raise subtitle.SubtitleProcessError("找不到原视频或语音字幕文件")
    destination = Path(out) if out is not None else _output_path(video)
    exported_chat = destination.with_name(destination.stem + "_弹幕.ass")
    if destination.resolve() == video.resolve() or destination.exists():
        raise subtitle.SubtitleProcessError("输出文件已存在，请选择其他文件名：%s" % destination)
    if jsonl is not None and exported_chat.exists():
        raise subtitle.SubtitleProcessError("弹幕字幕文件已存在，请选择其他文件名：%s" % exported_chat)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_cancelled(cancelled)
    source = _probe(ffmpeg, video, cancelled)
    source_subtitles = [s for s in source["streams"] if s.get("codec_type") == "subtitle"]
    if combined and jsonl is None and source_subtitles:
        raise subtitle.SubtitleProcessError(
            "原视频已有字幕，但缺少本场弹幕 JSONL，无法安全生成同时显示轨。"
            "请找回弹幕文件，或选择「独立字幕轨」（原字幕轨仍会保留）。")
    try:
        duration = float(source["format"]["duration"])
        picture = next(s for s in source["streams"] if s.get("codec_type") == "video")
        width, height = int(picture["width"]), int(picture["height"])
    except (KeyError, StopIteration, ValueError, TypeError) as exc:
        raise subtitle.SubtitleProcessError("无法取得视频时长或分辨率") from exc
    cues = _read_cues(srt, duration)
    with tempfile.TemporaryDirectory(prefix=".lumina-speech-", dir=destination.parent) as temp:
        temp = Path(temp)
        ass, chat, rendered = temp / "combined.ass", temp / "danmaku.ass", temp / "output.mkv"
        normalized = temp / "speech.srt"
        _write_srt(cues, normalized)
        if combined or jsonl is not None:
            if progress:
                progress("正在合并语音和弹幕字幕…")
            build_combined_ass(srt, ass, width=width, height=height, duration=duration,
                               jsonl=jsonl, offset=offset, style=style,
                               danmaku_out=chat if jsonl is not None else None)
        _check_cancelled(cancelled)
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(video), "-i", str(normalized)]
        next_input = 2
        chat_input = next_input if jsonl is not None else None
        if chat_input is not None:
            command += ["-i", str(chat)]
            next_input += 1
        combined_input = next_input if combined else None
        if combined_input is not None:
            command += ["-i", str(ass)]
        command += ["-map", "0", "-map", "1:0"]
        if chat_input is not None:
            command += ["-map", "%d:0" % chat_input]
        if combined_input is not None:
            command += ["-map", "%d:0" % combined_input]
        command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
        for index, stream in enumerate(source_subtitles):
            # MP4 的 mov_text 无法直接放进 Matroska，保留其文本与元数据。
            if stream.get("codec_name") == "mov_text":
                command += ["-c:s:%d" % index, "srt"]
            command += ["-disposition:s:%d" % index, "-default"]
        speech_index = len(source_subtitles)
        metadata_language = {"zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor"}.get(
            language, language if re.fullmatch(r"[a-z]{3}", language or "") else "und")
        command += ["-c:s:%d" % speech_index, "srt", "-metadata:s:s:%d" % speech_index,
                    "title=" + SPEECH_TITLE, "-metadata:s:s:%d" % speech_index,
                    "language=" + metadata_language,
                    "-disposition:s:%d" % speech_index, "0" if combined else "default"]
        next_subtitle = speech_index + 1
        if chat_input is not None:
            command += ["-c:s:%d" % next_subtitle, "ass",
                        "-metadata:s:s:%d" % next_subtitle, "title=" + DANMAKU_TITLE,
                        "-metadata:s:s:%d" % next_subtitle, "language=chi",
                        "-disposition:s:%d" % next_subtitle, "0"]
            next_subtitle += 1
        if combined_input is not None:
            command += ["-c:s:%d" % next_subtitle, "ass",
                        "-metadata:s:s:%d" % next_subtitle, "title=" + COMBINED_TITLE,
                        "-metadata:s:s:%d" % next_subtitle, "language=" + metadata_language,
                        "-disposition:s:%d" % next_subtitle, "default"]
        command += ["-f", "matroska", str(rendered)]
        if progress:
            progress("正在封装语音字幕，保留原视频…")
        _run(command, cancelled)
        _validate(source, _probe(ffmpeg, rendered, cancelled),
                  1 + int(combined) + int(chat_input is not None))
        _check_cancelled(cancelled)
        chat_published = False
        try:
            # 同目录硬链接原子发布，目标若被其他任务抢占会失败，不覆盖用户文件。
            if chat_input is not None:
                os.link(chat, exported_chat)
                chat_published = True
            _check_cancelled(cancelled)
            os.link(rendered, destination)
        except (OSError, SpeechSubtitleCancelled) as exc:
            if chat_published:
                exported_chat.unlink(missing_ok=True)
            if isinstance(exc, SpeechSubtitleCancelled):
                raise
            raise subtitle.SubtitleProcessError("无法保存成品，原片已保留：%s" % exc) from exc
    return destination
