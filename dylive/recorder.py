"""总调度：盯开播、拉画面、收弹幕，三件事共用同一个时间基准。

时间基准很重要 —— 弹幕的 offset 是相对 ffmpeg 起录那一刻算的，所以录完之后
弹幕文件能直接对着视频播。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import messages, room, subtitle, video
from .messages import DEFAULT_KINDS, Event
from .utils import hms, safe_name, stamp
from .writers import ConsoleWriter, JsonlWriter, Writer, WriterGroup, XmlWriter

log = logging.getLogger("recorder")

# 连续这么多次查不到直播间才放弃 —— 单纯断网不该让录制退出
MAX_LOOKUP_FAILURES = 30


@dataclass
class Status:
    """给界面轮询用的状态快照。"""

    running: bool = False
    waiting: bool = False                   # 守候中（还没开播）
    info: Optional["room.RoomInfo"] = None
    elapsed: float = 0.0
    counts: Dict[str, int] = field(default_factory=dict)
    files: List[Tuple[str, int]] = field(default_factory=list)
    post: str = ""                          # 录完之后的收尾进度（封装字幕等）
    danmaku_note: str = ""                  # 弹幕这一路的异常（被风控挡住等）

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.files)


@dataclass
class Options:
    target: str
    out_dir: Path = Path("output")
    engine: str = "browser"                 # browser | native
    quality: str = "origin"
    prefer: str = "flv"                     # flv | hls
    container: str = "mp4"
    segment_seconds: int = 0

    record_video: bool = True
    record_danmaku: bool = True
    kinds: tuple = DEFAULT_KINDS
    write_xml: bool = True
    show_console: bool = True

    ffmpeg: str = ""
    headless: bool = True
    block_media: bool = True
    user_data_dir: str = ""
    cookie: str = ""
    proxy: str = ""

    embed_subtitle: bool = False            # 录完把弹幕作为软字幕封进 mkv
    subtitle_replace: bool = True           # 封装成功后用 mkv 替换原视频
    subtitle_size: int = 48
    subtitle_duration: float = 10.0
    subtitle_reserve: float = 0.4
    subtitle_kinds: tuple = subtitle.DEFAULT_KINDS

    watch: bool = False                     # 未开播时守候到开播
    poll_interval: int = 60                 # 守候轮询间隔（秒）
    check_interval: int = 30                # 录制中确认是否还在播的间隔（秒）
    retry_delay: int = 10                   # 断流后重试间隔（秒）
    extra_ffmpeg_args: List[str] = field(default_factory=list)


class Recorder:
    def __init__(self, opts: Options, hub=None):
        # hub 由多房间管理器注入：所有房间共用一个 Chromium。
        # 不传就自己开一个，命令行单房间走这条路。
        self.hub = hub
        self.opts = opts
        self.session = room.make_session(opts.cookie)
        self.web_rid = room.parse_target(opts.target, self.session)
        self.ffmpeg = video.find_ffmpeg(opts.ffmpeg) if opts.record_video else ""

        # 界面可以往这里塞自己的 writer（每场录制开始时挂进 WriterGroup）
        self.extra_writers: List[Writer] = []

        self._stop = threading.Event()
        self._ended_by_anchor = threading.Event()
        self._page_info: Optional[room.RoomInfo] = None
        self._counter: Counter = Counter()
        self._writers = WriterGroup()
        self._start_time = 0.0
        self._status_lock = threading.Lock()
        self._live_info: Optional[room.RoomInfo] = None
        self._prefix: Optional[Path] = None
        self._recording = False
        self._post = ""
        self._engine = None             # 弹幕引擎，界面要读它的连接状态
        self._runs: List[Tuple[video.VideoRecorder, float]] = []

    # -- 对外 -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def status(self) -> Status:
        """线程安全的状态快照，供界面每秒轮询一次。"""
        with self._status_lock:
            recording, prefix, info = self._recording, self._prefix, self._live_info
            elapsed = (time.time() - self._start_time) if recording else 0.0
            counts = dict(self._counter)
            post = self._post

        files: List[Tuple[str, int]] = []
        if prefix is not None:
            try:
                files = sorted(
                    (p.name, p.stat().st_size) for p in prefix.parent.iterdir()
                    if p.is_file() and p.name.startswith(prefix.name))
            except OSError:
                pass
        return Status(running=recording, waiting=not recording and not self.stopping,
                      info=info, elapsed=elapsed, counts=counts, files=files, post=post,
                      danmaku_note=self._danmaku_note(recording))

    def _danmaku_note(self, recording: bool) -> str:
        """弹幕这一路有没有出问题。没问题返回空串。

        画面和弹幕是两路独立的管道，弹幕断了画面照录 —— 所以它不能算「错误
        状态」，但必须让用户当场看见，否则就是录完一整场才发现 jsonl 是空的。
        """
        engine = self._engine
        if engine is None or not recording:
            return ""
        if getattr(engine, "blocked", ""):
            return ("被抖音风控挡在验证页，收不到弹幕。画面不受影响，仍在正常录制。"
                    "到「设置 → 显示浏览器窗口」，在弹出的窗口里自己过一次验证即可。")
        connected = getattr(engine, "connected", None)
        if connected is not None and not connected.is_set():
            waited = time.time() - getattr(engine, "attached_at", self._start_time)
            if waited > 45:
                # 说不准是什么原因，就别乱开药方 —— 只讲事实，让用户去看日志
                return ("弹幕连接一直没建立，本场可能一条弹幕都收不到。"
                        "画面不受影响，仍在正常录制；具体原因见「运行日志」。")
        return ""

    def run(self) -> int:
        log.info("目标直播间：%s", self.web_rid)
        sessions = 0
        while not self._stop.is_set():
            info = room.fetch(self.web_rid, self.session)
            log.info("%s", info.describe())

            if not info.ok:
                # 查询本身失败（断网/风控），不是「没开播」，别当成结束
                if not self.opts.watch:
                    log.error("查不到直播间信息，检查网络后重试。")
                    return 1 if sessions == 0 else 0
                log.warning("查询失败，%d 秒后重试…", self.opts.poll_interval)
                self._stop.wait(self.opts.poll_interval)
                continue

            if not info.living:
                if not self.opts.watch:
                    log.error("当前未开播。加 --watch 可以守到开播自动开录。")
                    return 1 if sessions == 0 else 0
                log.info("未开播，%d 秒后再看一次…", self.opts.poll_interval)
                self._stop.wait(self.opts.poll_interval)
                continue

            self._record_session(info)
            sessions += 1

            if not self.opts.watch or self._stop.is_set():
                break
            log.info("本场结束，继续守候下一场…")
            self._stop.wait(self.opts.poll_interval)
        return 0

    # -- 单场录制 ---------------------------------------------------------

    def _record_session(self, info: room.RoomInfo) -> None:
        self._ended_by_anchor.clear()
        self._page_info = None
        self._counter.clear()

        self._start_time = time.time()
        prefix = self._make_prefix(info)
        log.info("输出前缀：%s", prefix)
        with self._status_lock:
            self._recording, self._prefix, self._live_info = True, prefix, info

        engine = self._start_danmaku(info, prefix) if self.opts.record_danmaku else None
        self._engine = engine           # 界面要从它身上读弹幕连没连上
        try:
            if self.opts.record_video:
                self._video_loop(info, prefix)
            else:
                self._danmaku_only_loop()
        except KeyboardInterrupt:
            log.info("收到 Ctrl-C，正在收尾…")
            self._stop.set()
        finally:
            with self._status_lock:
                self._recording = False
            if engine is not None:
                engine.stop()
            self._writers.close()          # 字幕要读 jsonl，必须等它先落完盘
            self._report(prefix)
            self._embed_subtitles(prefix)

    def _make_prefix(self, info: room.RoomInfo) -> Path:
        folder = self.opts.out_dir / safe_name(info.nickname or self.web_rid, 40)
        name = "%s_%s" % (stamp(self._start_time), safe_name(info.title, 50))
        folder.mkdir(parents=True, exist_ok=True)
        return folder / name

    # -- 弹幕 -------------------------------------------------------------

    def _start_danmaku(self, info: room.RoomInfo, prefix: Path):
        self._writers.add(JsonlWriter(prefix.with_suffix(".jsonl")))
        if self.opts.write_xml:
            self._writers.add(XmlWriter(prefix.with_suffix(".xml"),
                                        info.room_id or self.web_rid))
        if self.opts.show_console:
            self._writers.add(ConsoleWriter())
        for extra in self.extra_writers:
            self._writers.add(extra)

        if self.opts.engine == "native":
            from .danmaku_native import NativeEngine
            engine = NativeEngine(
                room_id=info.room_id,
                on_event=self._on_event,
                cookie=self.opts.cookie or self._session_cookie(),
                proxy=self.opts.proxy,
                reconnect_delay=self.opts.retry_delay,
            )
        elif self.hub is not None:
            channel = self.hub.attach(self.web_rid, self._on_event, self._on_room_info)
            channel.start_time = self._start_time
            return channel          # detach 由 channel.stop() 完成，不会关掉共享浏览器
        else:
            from .danmaku_browser import BrowserEngine
            engine = BrowserEngine(
                web_rid=self.web_rid,
                on_event=self._on_event,
                on_room_info=self._on_room_info,
                headless=self.opts.headless,
                block_media=self.opts.block_media,
                user_data_dir=self.opts.user_data_dir or None,
                cookie=self.opts.cookie,
                proxy=self.opts.proxy,
            )

        engine.start_time = self._start_time
        engine.start()
        return engine

    def _session_cookie(self) -> str:
        return "; ".join("%s=%s" % (c.name, c.value) for c in self.session.cookies)

    def _on_event(self, ev: Event) -> None:
        if ev.kind not in self.opts.kinds:
            return
        self._counter[ev.kind] += 1
        self._writers.write(ev)
        if messages.is_stream_ended(ev):
            log.info("收到下播消息（status=%s）", ev.extra.get("status"))
            self._ended_by_anchor.set()

    def _on_room_info(self, info: room.RoomInfo) -> None:
        """浏览器页面自己请求到的 enter 响应，比我们直接调接口更可信。"""
        if info.flv or info.hls:
            self._page_info = info

    # -- 画面 -------------------------------------------------------------

    def _video_loop(self, info: room.RoomInfo, prefix: Path) -> None:
        part = 0
        offline_streak = 0
        self._runs = []
        while not self._stop.is_set() and not self._ended_by_anchor.is_set():
            url = self._stream_url(info)
            if not url:
                log.error("拿不到拉流地址，放弃本场画面录制")
                return

            out = prefix if part == 0 else Path(str(prefix) + "_r%d" % part)
            rec = video.VideoRecorder(
                self.ffmpeg, url, out,
                container=self.opts.container,
                segment_seconds=self.opts.segment_seconds,
                extra_args=self.opts.extra_ffmpeg_args,
            )
            # 记下这一路是从整场的第几秒开始的 —— 断流重连后的片段，
            # 弹幕字幕要按这个值平移，否则第二段开始就全错位
            self._runs.append((rec, max(0.0, time.time() - self._start_time)))
            rec.start()
            self._watch_ffmpeg(rec)
            rec.stop()

            if self._stop.is_set() or self._ended_by_anchor.is_set():
                return

            # ffmpeg 自己退了。要区分三种情况：确认下播（收工）、查不到（八成是
            # 断网，正是最该重试的时候）、还在播（换个新地址接着录）。
            fresh = room.fetch(self.web_rid, self.session)
            if fresh.ok:
                offline_streak = 0
                if not fresh.living:
                    log.info("主播已下播，本场结束")
                    return
                info = fresh
            else:
                offline_streak += 1
                if offline_streak >= MAX_LOOKUP_FAILURES:
                    log.error("连续 %d 次查不到直播间，放弃本场", offline_streak)
                    return
                log.warning("查不到直播间（第 %d 次），按断网处理，继续重试",
                            offline_streak)
                # 地址多半也过期了，但没有更好的选择，先拿旧的试

            part += 1
            log.warning("画面中断，%d 秒后重连（第 %d 次）", self.opts.retry_delay, part)
            self._stop.wait(self.opts.retry_delay)

    def _stream_url(self, info: room.RoomInfo) -> Optional[str]:
        """挑拉流地址。直接查到的就用，查不到才等浏览器页面兜底。"""
        source = info
        if not (info.flv or info.hls):
            # 接口和页面解析都失败时，等浏览器把它自己的 enter 响应送过来
            deadline = time.time() + 45
            while time.time() < deadline and self._page_info is None:
                if self._stop.is_set() or self.opts.engine != "browser":
                    return None
                time.sleep(0.5)
            if self._page_info is None:
                return None
            source = self._page_info

        url = source.pick(self.opts.quality, self.opts.prefer)
        if url:
            log.info("使用%s流：%s", self.opts.prefer.upper(), url.split("?")[0])
        return url

    def _watch_ffmpeg(self, rec: video.VideoRecorder) -> None:
        """盯着 ffmpeg，同时定期确认主播还在播。"""
        next_check = time.time() + self.opts.check_interval
        while rec.alive:
            if self._stop.is_set() or self._ended_by_anchor.is_set():
                return
            if time.time() >= next_check:
                next_check = time.time() + self.opts.check_interval
                fresh = room.fetch(self.web_rid, self.session)
                if not fresh.ok:
                    # 查不到不代表下播。ffmpeg 还活着就说明流还在，接着录
                    log.debug("存活检查失败，忽略（ffmpeg 仍在运行）")
                elif not fresh.living:
                    log.info("轮询发现已下播")
                    self._ended_by_anchor.set()
                    return
            time.sleep(1.0)

    def _danmaku_only_loop(self) -> None:
        log.info("只录弹幕（--no-video）")
        while not self._stop.is_set() and not self._ended_by_anchor.is_set():
            time.sleep(1.0)

    # -- 字幕 -------------------------------------------------------------

    def _collect_parts(self) -> Tuple[List[Path], List[float]]:
        """列出本场所有视频文件及各自相对整场开始的秒数。"""
        videos: List[Path] = []
        offsets: List[float] = []
        for rec, base in self._runs:
            acc = base
            for path in rec.files():
                videos.append(path)
                offsets.append(acc)
                acc += subtitle.probe(self.ffmpeg, path).get("duration", 0.0)
        return videos, offsets

    def _embed_subtitles(self, prefix: Path) -> None:
        if not (self.opts.embed_subtitle and self.opts.record_video
                and self.opts.record_danmaku):
            return
        jsonl = prefix.with_suffix(".jsonl")
        if not jsonl.is_file():
            return
        videos, offsets = self._collect_parts()
        if not videos:
            return

        def note(msg: str) -> None:
            with self._status_lock:
                self._post = msg

        note("正在生成弹幕字幕…")
        try:
            subtitle.process(
                self.ffmpeg, jsonl, videos, offsets,
                style=subtitle.Style(size=self.opts.subtitle_size,
                                     duration=self.opts.subtitle_duration,
                                     reserve=self.opts.subtitle_reserve),
                kinds=self.opts.subtitle_kinds,
                replace=self.opts.subtitle_replace,
                progress=note)
        except Exception as exc:            # noqa: BLE001 - 字幕失败不该影响录像
            log.error("封装弹幕字幕失败：%s", exc)
        finally:
            note("")

    # -- 收尾 -------------------------------------------------------------

    def _report(self, prefix: Path) -> None:
        elapsed = time.time() - self._start_time
        total = sum(self._counter.values())
        detail = "，".join("%s %d" % (k, v) for k, v in self._counter.most_common())
        log.info("本场时长 %s，弹幕事件 %d 条%s",
                 hms(elapsed), total, ("（" + detail + "）") if detail else "")
        # 前缀匹配而非 glob：标题里的 [ ] 会被 glob 当成字符类
        produced = sorted(p for p in prefix.parent.iterdir()
                          if p.is_file() and p.name.startswith(prefix.name))
        for p in produced:
            log.info("  %s  %.1f MB", p.name, p.stat().st_size / 1048576)
