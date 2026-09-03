"""把弹幕做成 ASS 字幕，并封装进录像。

用**软字幕**：字幕作为一条轨道封进 mkv，视频音频原样 `-c copy` 搬过去，
不重新编码 —— 画质无损、几小时的录像一两分钟就完事，播放器里还能随时开关。
（硬字幕要重编码，两小时 1080p 纯 CPU 得跑半小时以上，而且关不掉。）

mp4 容器不支持 ASS，所以带字幕的成品统一是 mkv。

分片和断流重连会让一场录制产出多个文件，而弹幕的 offset 是相对**整场**开始算的，
所以每个文件都要按它自己的起始时刻把字幕平移一遍，否则第二段开始就全错位。
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .paths import NO_WINDOW, subtitle_font

log = logging.getLogger("subtitle")

DEFAULT_KINDS = ("chat", "emoji")

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DM,{font},{size},&H{alpha}FFFFFF,&H{alpha}FFFFFF,&H{alpha}000000,&H{alpha}000000,0,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class Style:
    width: int = 1920
    height: int = 1080
    font: str = ""              # 空 = 按当前系统挑一个自带的中文字体
    size: int = 48
    duration: float = 10.0      # 一条弹幕横穿屏幕的秒数
    opacity: float = 0.85
    reserve: float = 0.4        # 屏幕下方留白比例，别挡住主播


# --------------------------------------------------------------------------
# 读弹幕
# --------------------------------------------------------------------------

def load_events(jsonl: Path, kinds: Sequence[str] = DEFAULT_KINDS) -> List[dict]:
    wanted = set(kinds)
    out: List[dict] = []
    try:
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue            # 录制中途断电可能留半行
                if ev.get("kind") in wanted and ev.get("content"):
                    out.append(ev)
    except OSError as exc:
        log.warning("读取 %s 失败：%s", jsonl.name, exc)
    return out


# --------------------------------------------------------------------------
# 生成 ASS
# --------------------------------------------------------------------------

def _text_width(text: str, size: int) -> float:
    """中文按一个字宽、其余按半个算，够 ASS 排版用了。"""
    return sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in text) * size


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%05.2f" % (int(h), int(m), s)


def build_ass(events: Iterable[dict], out: Path, style: Optional[Style] = None,
              shift: float = 0.0, window: Optional[Tuple[float, float]] = None) -> int:
    """写一份 ASS。

    shift  ——  所有时间减去这个秒数（用于分片：第二段的 0 秒对应整场的第 N 秒）
    window ——  只保留 [起, 止) 区间内的弹幕，None 表示不限
    """
    st = style or Style()
    alpha = "%02X" % int(max(0.0, min(1.0, 1.0 - st.opacity)) * 255)
    # 字体名是给播放器用的，得是**播放这台机器**上真有的字体，所以按系统挑
    lines = [ASS_HEADER.format(w=st.width, h=st.height,
                               font=st.font or subtitle_font(),
                               size=st.size, alpha=alpha)]

    lanes = max(1, int(st.height * (1.0 - st.reserve)) // int(st.size * 1.2))
    lane_h = int(st.size * 1.2)
    tail_enter = [0.0] * lanes      # 上一条弹幕尾巴进屏的时间
    leave = [0.0] * lanes           # 上一条弹幕完全离屏的时间

    written = 0
    for ev in events:
        raw = float(ev.get("offset", 0.0))
        if window is not None and not (window[0] <= raw < window[1]):
            continue
        t = raw - shift
        if t < -st.duration:        # 完全在这一段之前，跳过
            continue
        t = max(0.0, t)

        text = ev["content"].replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        text = text.replace("\n", " ").replace("\r", " ")
        w = _text_width(text, st.size)
        speed = (st.width + w) / st.duration

        lane = None
        for i in range(lanes):
            # 前一条的尾巴已进屏，且这条追不上它
            if t >= tail_enter[i] and t + st.width / speed >= leave[i]:
                lane = i
                break
        if lane is None:
            lane = min(range(lanes), key=lambda i: leave[i])
        tail_enter[lane] = t + w / speed
        leave[lane] = t + st.duration

        y = lane * lane_h
        lines.append(
            "Dialogue: 0,%s,%s,DM,,0,0,0,,{\\move(%d,%d,%d,%d)}%s\n"
            % (_ass_time(t), _ass_time(t + st.duration), st.width, y, -int(w), y, text))
        written += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8-sig")
    return written


# --------------------------------------------------------------------------
# 封装
# --------------------------------------------------------------------------

def probe(ffmpeg: str, path: Path) -> Dict[str, float]:
    """拿视频的时长和分辨率。ffprobe 和 ffmpeg 在同一个目录。"""
    ffprobe = str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix))
    if not Path(ffprobe).is_file():
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffprobe %s 失败：%s", path.name, exc)
        return {}
    info: Dict[str, float] = {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition("=")
        try:
            info[key.strip()] = float(value)
        except ValueError:
            continue
    return info


def mux(ffmpeg: str, video: Path, ass: Path, out: Path) -> bool:
    """把 ASS 作为一条字幕轨封进 mkv，音视频原样搬运不重编码。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-i", str(ass),
           "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0",
           "-c", "copy", "-c:s", "ass",
           "-metadata:s:s:0", "title=弹幕",
           "-metadata:s:s:0", "language=chi",
           "-disposition:s:0", "default",
           str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=3600, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("封装 %s 失败：%s", video.name, exc)
        return False
    if proc.returncode != 0:
        log.warning("封装 %s 失败：%s", video.name,
                    proc.stderr.decode("utf-8", "replace").strip()[:300])
        return False
    return True


def _looks_sane(ffmpeg: str, source: Path, result: Path, slack: float) -> bool:
    """删原文件前确认成品是真能播的：体积没缩水、时长没变短、字幕轨在。

    只查「没变短」而不是「一模一样」—— 字幕轨要比最后一条弹幕再延续一个弹幕
    时长，容器时长取所有轨道的最大值，所以成品比原视频长几秒是正常的。
    """
    if not result.is_file() or result.stat().st_size < source.stat().st_size * 0.9:
        return False
    a, b = probe(ffmpeg, source), probe(ffmpeg, result)
    if not a.get("duration") or not b.get("duration"):
        return False
    if b["duration"] < a["duration"] - 2.0:
        return False
    if b["duration"] > a["duration"] + slack + 10.0:
        return False
    return True


# --------------------------------------------------------------------------
# 一场录制的完整处理
# --------------------------------------------------------------------------

VIDEO_SUFFIXES = (".mp4", ".flv", ".ts", ".mkv")


def find_videos(prefix: Path) -> List[Path]:
    """按前缀找出这场录制的所有视频文件，按名字排序即是时间顺序。

    用前缀匹配而不是 glob：直播间标题里的 [ ] 是 glob 元字符。
    """
    try:
        return sorted(p for p in prefix.parent.iterdir()
                      if p.is_file() and p.name.startswith(prefix.name)
                      and p.suffix.lower() in VIDEO_SUFFIXES
                      and not p.stem.endswith("_弹幕版"))
    except OSError:
        return []


def process(ffmpeg: str, jsonl: Path, videos: Sequence[Path],
            offsets: Optional[Sequence[float]] = None,
            style: Optional[Style] = None,
            kinds: Sequence[str] = DEFAULT_KINDS,
            replace: bool = True,
            keep_ass: bool = False,
            progress: Optional[Callable[[str], None]] = None) -> List[Path]:
    """给一场录制的每个视频文件配上弹幕字幕。

    offsets 是每个视频相对整场开始的秒数；不给就用各文件时长依次累加推算
    （对分片是准的，对断流重连中间的空档会有偏差，所以录制器会把真实值传进来）。
    返回生成的成品路径。
    """
    def say(msg: str) -> None:
        log.info("%s", msg)
        if progress is not None:
            progress(msg)

    events = load_events(jsonl, kinds)
    if not events:
        say("%s 里没有可用弹幕，跳过" % jsonl.name)
        return []
    if not videos:
        say("没找到对应的视频文件，跳过")
        return []

    if offsets is None:
        offsets = []
        acc = 0.0
        for path in videos:
            offsets.append(acc)
            acc += probe(ffmpeg, path).get("duration", 0.0)

    results: List[Path] = []
    for i, video in enumerate(videos):
        info = probe(ffmpeg, video)
        duration = info.get("duration", 0.0)
        start = float(offsets[i]) if i < len(offsets) else 0.0
        window = (start, start + duration) if duration else (start, float("inf"))

        st = style or Style()
        if info.get("width") and info.get("height"):
            st = Style(**{**st.__dict__,
                          "width": int(info["width"]), "height": int(info["height"])})

        ass = video.with_suffix(".ass")
        count = build_ass(events, ass, st, shift=start, window=window)
        if not count:
            say("%s 这一段没有弹幕，跳过" % video.name)
            ass.unlink(missing_ok=True)
            continue

        out = video.with_name(video.stem + "_弹幕版.mkv")
        say("正在封装 %s（%d 条弹幕）…" % (video.name, count))
        ok = mux(ffmpeg, video, ass, out)
        if not keep_ass:
            ass.unlink(missing_ok=True)
        if not ok:
            out.unlink(missing_ok=True)
            continue

        if replace:
            if _looks_sane(ffmpeg, video, out, st.duration):
                final = video.with_suffix(".mkv")
                try:
                    video.unlink()
                    out.replace(final)
                    out = final
                except OSError as exc:
                    say("替换原文件失败（保留两份）：%s" % exc)
            else:
                say("成品校验没过，原文件保留：%s" % out.name)
        results.append(out)
        say("完成 %s" % out.name)
    return results
