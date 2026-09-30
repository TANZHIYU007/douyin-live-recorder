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
import math
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, replace as replace_style
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import runtime
from .paths import NO_WINDOW, subtitle_font
from .utils import files_with_prefix

log = logging.getLogger("subtitle")

DEFAULT_KINDS = ("chat", "emoji")

# ffmpeg 的 ass 编码器扛不住太多事件：实测 5 万条 Dialogue 能过、16 万条就
# 报「Cannot allocate memory」。留一半余量。
MAX_DIALOGUE = 50000

SCROLL = "scroll"           # 从右往左飘过屏幕的传统弹幕
CHAT = "chat"               # 左下角堆成一列的直播间聊天流

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# 滚动弹幕：描边字，锚点左上（Alignment=7），位置全靠 \move 现算
STYLE_SCROLL = ("Style: DM,{font},{size},&H{alpha}FFFFFF,&H{alpha}FFFFFF,"
                "&H{alpha}000000,&H{alpha}000000,0,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1")

# 聊天流的气泡拆成两条 Dialogue 叠着画，原因见下：
#
# BorderStyle=3 会让 libass 在文字后面铺一块底色，就是抖音那种小气泡。但它是
# **按颜色段**画的 —— 用户名和内容一旦用 \c 分成两段，就会画出两个框，交界
# 处叠一起，半透明底色在那里深一块，看着像渲染坏了。
#
# 所以底框单独一条：整行**不分段**（单一颜色），只画一个框；文字用 \1a&HFF&
# 设成全透明，只留框。彩色文字放到上面一层，那一层不画框。
# 这样气泡尺寸是 libass 按真实字形算的，永远和文字贴合，也不会有接缝。
STYLE_CHAT = (
    "Style: DM,{font},{size},&H{alpha}FFFFFF,&H{alpha}FFFFFF,"
    "&H{box}000000,&H{box}000000,0,0,0,0,100,100,0,0,3,{pad},0,1,0,0,0,1\n"
    "Style: DMT,{font},{size},&H{alpha}FFFFFF,&H{alpha}FFFFFF,"
    "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,1,0,0,0,1")


@dataclass
class Style:
    width: int = 1920
    height: int = 1080
    font: str = ""              # 空 = 按当前系统挑一个自带的中文字体
    size: int = 48
    duration: float = 10.0      # 滚动弹幕横穿屏幕的秒数；聊天流不按时间消失
    opacity: float = 0.85
    reserve: float = 0.4        # 滚动样式专用：屏幕下方留白比例，别挡住主播

    # 弹幕整体往后推的秒数。弹幕和画面走的是两条完全不同的链路：WebSocket 几乎
    # 是实时的，而 FLV/HLS 要经过 CDN 的缓冲，通常慢 3~8 秒。两边用同一个零点
    # 对齐之后，弹幕仍然会系统性地**早于**画面 —— 观众还没看到主播说那句话，
    # 回应已经飘过去了。这个值把弹幕整体往后推，抵掉那段拉流延迟。
    #
    # 只在生成字幕时平移，jsonl 里存的永远是原始 offset：延迟估错了可以重新
    # 生成，不会污染原始记录。
    delay: float = 0.0

    # -- 聊天流样式专用 --
    mode: str = SCROLL
    lines: int = 8              # 左下角最多同时显示几条
    box_opacity: float = 0.45   # 气泡底色的不透明度
    name_color: str = "A9D3FF"  # 用户名颜色（RRGGBB），抖音那种淡蓝
    chat_width: float = 0.0     # 一条最宽占屏幕的比例；0 = 按画面比例自动
    margin_x: float = 0.035     # 距左边的比例
    margin_y: float = 0.08      # 最底下那条距底边的比例
    max_rate: float = 2.5       # 每秒最多显示几条，超了丢（看不过来，也放不下）

    @classmethod
    def from_options(cls, options) -> "Style":
        """录后自动封装与界面补做共用同一组字幕选项。"""
        return cls(mode=options.subtitle_style, size=options.subtitle_size,
                   duration=options.subtitle_duration, reserve=options.subtitle_reserve,
                   delay=options.subtitle_delay)


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
                if (isinstance(ev, dict) and ev.get("kind") in wanted
                        and isinstance(ev.get("content"), str) and ev["content"]):
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
    font = st.font or subtitle_font()

    if st.mode == CHAT:
        box = "%02X" % int(max(0.0, min(1.0, 1.0 - st.box_opacity)) * 255)
        style_line = STYLE_CHAT.format(font=font, size=st.size, alpha=alpha,
                                       box=box, pad=max(2, int(st.size * 0.30)))
    else:
        style_line = STYLE_SCROLL.format(font=font, size=st.size, alpha=alpha)
    head = ASS_HEADER.format(w=st.width, h=st.height, style_line=style_line)

    # 分片开始时，上一片末尾的聊天仍应留在屏幕上，直到后续弹幕将它顶走。
    if st.mode == CHAT:
        events = list(events)
    picked = _pick(events, st, shift, window)
    if st.mode == CHAT:
        initial = _chat_history(events, st, window[0]) if window is not None else []
        end_at = (window[1] - shift) if window and math.isfinite(window[1]) else None
        body = _chat_lines(picked, st, initial=initial, end_at=end_at)
    else:
        initial = []
        body = _scroll_lines(picked, st)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(head + "".join(body), encoding="utf-8-sig")
    return len(picked) + len(initial)


def _pick(events, st: Style, shift: float,
          window: Optional[Tuple[float, float]]) -> List[Tuple[float, dict]]:
    """筛掉不属于这一段的弹幕，并把时间平移到这一段自己的 0 点。

    延迟补偿要在**分段之前**加上去：推后几秒之后，本来卡在上一段末尾的弹幕
    应该落到下一段开头。先补偿再按窗口切，顺序反了第二段开头就会缺一截。
    """
    out: List[Tuple[float, dict]] = []
    for ev in events:
        try:
            moment = float(ev.get("offset", 0.0)) + st.delay
        except (TypeError, ValueError):
            continue                # offset 坏了就跳过这一条，不要整场失败
        if not math.isfinite(moment):
            continue
        if window is not None and not (window[0] <= moment < window[1]):
            continue
        t = moment - shift
        if t < -st.duration:        # 完全在这一段之前，跳过
            continue
        out.append((max(0.0, t), ev))
    out.sort(key=lambda p: p[0])    # 聊天流按时间堆叠，顺序不能乱
    return out


def _escape(text: str) -> str:
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return text.replace("\n", " ").replace("\r", " ").strip()


def _scroll_lines(picked: List[Tuple[float, dict]], st: Style) -> List[str]:
    """传统弹幕：从右边飘进来、往左边飘出去，按轨道错开避免叠字。"""
    lines: List[str] = []
    lanes = max(1, int(st.height * (1.0 - st.reserve)) // int(st.size * 1.2))
    lane_h = int(st.size * 1.2)
    tail_enter = [0.0] * lanes      # 上一条弹幕尾巴进屏的时间
    leave = [0.0] * lanes           # 上一条弹幕完全离屏的时间

    for t, ev in picked:
        text = _escape(ev["content"])
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
    return lines


def _chat_history(events: Iterable[dict], st: Style, before: float) -> List[dict]:
    """取分片起点前仍在聊天栈中的消息，沿用聊天流的限速规则。"""
    earlier: List[Tuple[float, dict]] = []
    for ev in events:
        try:
            moment = float(ev.get("offset", 0.0)) + st.delay
        except (TypeError, ValueError):
            continue
        if math.isfinite(moment) and moment < before:
            earlier.append((moment, ev))
    earlier.sort(key=lambda item: item[0])

    recent = deque(maxlen=max(1, st.lines))
    gap = 1.0 / max(0.1, st.max_rate)
    last = -float("inf")
    for moment, ev in earlier:
        if moment - last >= gap:
            recent.append(ev)
            last = moment
    return list(recent)


def _chat_lines(picked: List[Tuple[float, dict]], st: Style,
                initial: Sequence[dict] = (), end_at: Optional[float] = None) -> List[str]:
    """抖音直播间那种左下角聊天流。

    每条新弹幕让整栈更新一次；安静时旧弹幕一直留着。每个状态只写底框和
    文字两条 Dialogue，事件量不会随着停留时间增长。多行文本由 libass
    排版，每行各画一个气泡；锚点在左下，新消息自然把旧消息顶上去。
    """
    shown = _thin(picked, st)
    if not shown and not initial:
        return []

    x = int(st.width * st.margin_x)
    y = int(st.height * (1.0 - st.margin_y))
    line_h = int(st.size * 1.45)
    # 竖屏（手机直播）横向像素少，聊天区得占得宽一些才放得下几个字；横屏
    # （游戏直播）反过来，占太宽会盖住画面主体
    frac = st.chat_width or (0.62 if st.height > st.width else 0.38)
    max_w = st.width * frac
    name_bgr = _bgr(st.name_color)
    stack = deque((_compose(ev, st, max_w, name_bgr) for ev in initial),
                  maxlen=max(1, st.lines))
    # 分片继承的聊天从 0 秒起显示；随后只在新消息到来时切换状态。
    states: List[Tuple[float, List[Tuple[str, str]], bool]] = []
    if stack:
        states.append((0.0, list(stack), False))
    for t, ev in shown:
        stack.append(_compose(ev, st, max_w, name_bgr))
        states.append((t, list(stack), True))

    lines: List[str] = []
    for i, (start, content, arrived) in enumerate(states):
        end = states[i + 1][0] if i + 1 < len(states) else end_at
        if end is None:
            # 单独导出 ASS 时不知道视频长度，保留原来的末尾时长兜底。
            end = start + st.duration
        if end - start < 0.04:
            continue
        plain = "\\N".join(item[0] for item in content)
        colored = "\\N".join(item[1] for item in content)
        if arrived:
            # 有新消息进来：整栈从低一行的位置滑上来，看着就是被顶上去的
            tags = "\\move(%d,%d,%d,%d,0,150)" % (x, y + line_h, x, y)
        else:
            tags = "\\pos(%d,%d)" % (x, y)
        a, b = _ass_time(start), _ass_time(end)
        # 底框在下、文字在上，两条用同一组定位标签，动起来才不会分家
        lines.append("Dialogue: 0,%s,%s,DM,,0,0,0,,{%s\\1a&HFF&}%s\n"
                     % (a, b, tags, plain))
        lines.append("Dialogue: 1,%s,%s,DMT,,0,0,0,,{%s}%s\n"
                     % (a, b, tags, colored))
    return lines


def _thin(picked: List[Tuple[float, dict]], st: Style) -> List[Tuple[float, dict]]:
    """限流。两个理由，缺一不可：

    1. 看不过来 —— 窗口就 lines 行，每秒涌进十几条的话每行只停零点几秒，
       谁也读不了。抖音自己的客户端在弹幕爆炸时也是丢的。
    2. ffmpeg 扛不住 —— ass 编码器实测五万条 Dialogue 左右就到顶了。

    先按最小间隔挑，再用总量兜底：万一还是超了就整体等距抽稀，宁可少放
    几条，也不能生成一个封装不进去的字幕。
    """
    gap = 1.0 / max(0.1, st.max_rate)
    kept: List[Tuple[float, dict]] = []
    last = -1e9
    for t, ev in picked:
        if t - last >= gap:
            kept.append((t, ev))
            last = t

    budget = max(1, MAX_DIALOGUE // 2 - 1)      # 为分片继承的初始状态留两条
    if len(kept) > budget:
        step = len(kept) / budget
        kept = [kept[int(i * step)] for i in range(budget)]
        log.info("弹幕太密，抽稀到 %d 条以保证能封装进视频", len(kept))
    return kept


def _bgr(rrggbb: str) -> str:
    """RRGGBB -> ASS 要的 BBGGRR。"""
    s = (rrggbb or "").lstrip("#")
    if len(s) != 6:
        return "FFD3A9"
    return s[4:6] + s[2:4] + s[0:2]


def _compose(ev: dict, st: Style, max_w: float,
             name_bgr: str) -> Tuple[str, str]:
    """拼成「用户名：内容」，返回 (不带颜色的, 带颜色的)。

    两份字符完全一样，只差颜色标签 —— 底框那条必须用不带颜色的版本，否则
    又会被分成两段、画出两个框。

    不做折行：折行会让每条的高度不固定，上面那套按格堆叠的算法就全乱了。
    """
    name = _escape(ev.get("user_name") or "")
    body = _escape(ev.get("content") or "")
    if not name:
        body = _clip(body, st.size, max_w)
        return body, body

    # 先截用户名，再拿**截完之后**的宽度去算正文预算 —— 反过来的话，遇到
    # 特别长的名字会算出负预算，正文反而一个字都不截
    name = _clip(name, st.size, max_w * 0.38)
    body = _clip(body, st.size, max_w - _text_width(name + "：", st.size))
    plain = "%s：%s" % (name, body)
    colored = "{\\c&H%s&}%s：{\\c&HFFFFFF&}%s" % (name_bgr, name, body)
    return plain, colored


def _clip(text: str, size: int, budget: float) -> str:
    """按预算截断并加省略号。预算再小也要截，不能整条放行。"""
    if not text or _text_width(text, size) <= budget:
        return text
    out = ""
    for ch in text:
        if _text_width(out + ch + "…", size) > budget:
            break
        out += ch
    return (out + "…") if out else text[0] + "…"


# --------------------------------------------------------------------------
# 封装
# --------------------------------------------------------------------------

def find_ffprobe(ffmpeg: str) -> str:
    """优先用打包版内置 ffprobe，再找 ffmpeg 同目录和系统 PATH。"""
    bundled = runtime.bundled_ffprobe()
    if bundled:
        return bundled
    sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def probe(ffmpeg: str, path: Path) -> Dict[str, float]:
    """拿视频的时长和分辨率。ffprobe 和 ffmpeg 在同一个目录。"""
    ffprobe = find_ffprobe(ffmpeg)
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
    """删原文件前检查成品体积和时长；这不是完整的解码验证。

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
    return [p for p in files_with_prefix(prefix)
            if p.suffix.lower() in VIDEO_SUFFIXES and not p.stem.endswith("_弹幕版")]


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

    acc = 0.0
    results: List[Path] = []
    for i, video in enumerate(videos):
        info = probe(ffmpeg, video)
        duration = info.get("duration", 0.0)
        start = acc if offsets is None else (float(offsets[i]) if i < len(offsets) else 0.0)
        acc += duration
        window = (start, start + duration) if duration else (start, float("inf"))

        st = style or Style()
        if info.get("width") and info.get("height"):
            st = replace_style(st, width=int(info["width"]), height=int(info["height"]))

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
                    if final != video and final.exists():
                        raise FileExistsError("目标文件已存在：%s" % final.name)
                    out.replace(final)
                    out = final
                    if video != final:
                        video.unlink()
                except OSError as exc:
                    say("替换原文件失败（保留两份）：%s" % exc)
            else:
                say("成品校验没过，原文件保留：%s" % out.name)
        results.append(out)
        say("完成 %s" % out.name)
    return results
