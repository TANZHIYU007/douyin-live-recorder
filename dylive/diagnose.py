"""逐段验证整条录制链路，尤其是弹幕那一路。

「弹幕收不到」是这个项目最常见也最难说清的故障 —— 因为画面还在正常录，
表面上一切正常，往往录完一整场才发现 .jsonl 是 0 字节。而断点可能在任何
一环：网络、房间查询、浏览器起不来、被风控挡在验证页、WebSocket 挂不上、
帧解不开、字段编号变了。

selftest 只管「环境齐不齐」，这里管「对着一个真实的直播间，到底走到哪一步
断了」。每一段都单独报结果，失败时给一句能照着做的话。

    python -m dylive.diagnose 123456789
    python -m dylive.diagnose 123456789 --seconds 90 --headful
    python -m dylive.diagnose 123456789 --no-video      # 只查弹幕那一路

报告同时写到 app_data_dir()/diagnose.txt。
"""

from __future__ import annotations

import argparse
import logging
import platform
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import paths, room, runtime
from .utils import UA, setup_console, setup_logging

log = logging.getLogger("diagnose")

# 弹幕这一段默认观察多久。热门房间几秒就有，冷清的房间可能一分钟才蹦一条，
# 所以「一条都没有」这个结论不能下得太早。
DEFAULT_SECONDS = 45
STREAM_PROBE_SECONDS = 4


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    hint: str = ""              # 失败时该怎么办，一句话

    def render(self) -> str:
        head = "[%s] %-10s %s" % ("通过" if self.ok else "失败", self.name, self.detail)
        return head + ("\n       → " + self.hint if self.hint and not self.ok else "")


@dataclass
class Report:
    steps: List[Step] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        log.info("%s", step.render().splitlines()[0])
        return step

    def render(self, target: str) -> str:
        lines = [
            "拾光 Lumina 链路诊断  %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "目标直播间: %s" % target,
            "运行方式: %s" % ("打包版" if runtime.is_frozen() else "源码"),
            "系统: %s %s / Python %s" % (platform.system(), platform.release(),
                                       platform.python_version()),
            "-" * 68,
        ]
        lines += [s.render() for s in self.steps]
        if self.counts:
            lines.append("-" * 68)
            lines.append("收到的事件：")
            for kind, n in self.counts.most_common():
                lines.append("  %-12s %d" % (kind, n))
        lines.append("-" * 68)
        lines.append("结论：%s" % ("整条链路都通，弹幕能正常录下来"
                                 if self.ok else "有环节没走通，见上面标「失败」的那几行"))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 各段检查
# --------------------------------------------------------------------------

def _step_ffmpeg() -> Step:
    from . import video
    try:
        path = video.find_ffmpeg()
    except RuntimeError as exc:
        return Step("ffmpeg", False, str(exc),
                    "Windows: winget install Gyan.FFmpeg；macOS: brew install ffmpeg")
    try:
        out = subprocess.run([path, "-version"], capture_output=True, timeout=20,
                             **paths.NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return Step("ffmpeg", False, "%s 跑不起来：%s" % (path, exc),
                    "确认这个路径下的 ffmpeg 没有损坏，或者换一份重装")
    first = out.stdout.decode("utf-8", "replace").splitlines()
    return Step("ffmpeg", out.returncode == 0, (first[0] if first else "无输出"))


def _step_network(sess) -> Step:
    import requests
    try:
        resp = sess.get("https://live.douyin.com/", timeout=15)
    except requests.RequestException as exc:
        return Step("网络", False, "连不上 live.douyin.com：%s" % str(exc)[:160],
                    "检查网络或代理；公司网络和部分地区可能需要 --proxy")
    names = [c.name for c in resp.cookies]
    ok = resp.status_code == 200
    detail = "HTTP %d，拿到 cookie %s" % (resp.status_code, names or "无")
    if ok and "ttwid" not in names and "ttwid" not in sess.cookies:
        return Step("网络", True, detail + "（没拿到 ttwid，风控可能会更严）")
    return Step("网络", ok, detail,
                "抖音把这次请求挡掉了，换个网络环境或挂代理再试")


def _step_room(target: str, sess) -> tuple[Step, Optional[room.RoomInfo]]:
    try:
        web_rid = room.parse_target(target, sess)
    except ValueError as exc:
        return Step("房间号", False, str(exc),
                    "直接填直播间号（纯数字），或者完整的 live.douyin.com 链接"), None

    info = room.fetch(web_rid, sess)
    if not info.ok:
        return Step("房间查询", False, "查不到 %s 的任何信息" % web_rid,
                    "接口和网页两条路都没走通 —— 多半是网络问题或风控，"
                    "不代表主播没开播"), info
    if not info.living:
        return Step("房间查询", False,
                    "%s（%s）当前未开播，status=%d" % (
                        info.nickname or web_rid, web_rid, info.status),
                    "换一个正在播的直播间来诊断 —— 没开播的房间没有弹幕可收，"
                    "这一步不能说明程序有问题"), info

    qualities = "/".join(sorted(set(info.flv) | set(info.hls))) or "无"
    return Step("房间查询", True,
                "%s | %s | 可用画质 %s" % (info.nickname or "-",
                                       (info.title or "-")[:30], qualities)), info


def _step_stream(info: room.RoomInfo, ffmpeg: str) -> Step:
    """真的拉几秒流下来，确认拿到的地址是活的。"""
    url = info.pick("origin", "flv")
    if not url:
        return Step("拉流", False, "没有可用的拉流地址",
                    "接口没给地址。浏览器引擎下录制器会用页面自己的 enter "
                    "响应兜底，但诊断这一步拿不到")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.ts"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-user_agent", UA,
               "-headers", "Referer: https://live.douyin.com/\r\n",
               "-rw_timeout", "15000000",
               "-i", url, "-t", str(STREAM_PROBE_SECONDS),
               "-c", "copy", "-f", "mpegts", str(out)]
        started = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=STREAM_PROBE_SECONDS + 30,
                                  **paths.NO_WINDOW)
        except subprocess.TimeoutExpired:
            return Step("拉流", False, "ffmpeg 超时，%ds 内没拉到数据" % STREAM_PROBE_SECONDS,
                        "地址可能已经过期，或者网络到 CDN 这一段不通")
        except OSError as exc:
            return Step("拉流", False, "ffmpeg 起不来：%s" % exc)

        size = out.stat().st_size if out.exists() else 0
        if proc.returncode != 0 or size == 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            return Step("拉流", False,
                        "拉不到数据（退出码 %s）：%s" % (
                            proc.returncode, err[-1][:160] if err else "无输出"),
                        "地址过期或被拒。换个画质试试，或者确认这个房间还在播")

        rate = size / max(0.1, time.time() - started) / 1024
        return Step("拉流", True, "%.1f 秒拿到 %.1f MB（约 %.0f KB/s），地址是活的"
                    % (STREAM_PROBE_SECONDS, size / 1048576, rate))


def _step_danmaku(web_rid: str, seconds: int, headless: bool, user_data_dir: str,
                  cookie: str, proxy: str, report: Report) -> List[Step]:
    """整条弹幕链路：浏览器 -> 页面 -> WebSocket -> 帧 -> 事件。"""
    from .browser_hub import BrowserHub
    from .messages import Event

    steps: List[Step] = []
    runtime.apply_env()
    hub = BrowserHub(headless=headless, user_data_dir=user_data_dir,
                     cookie=cookie, proxy=proxy)

    first_event: List[float] = []

    def on_event(ev: Event) -> None:
        if not first_event:
            first_event.append(time.time())
        report.counts[ev.kind] += 1

    started = time.time()
    channel = hub.attach(web_rid, on_event)
    try:
        if not hub.wait_ready(90):
            steps.append(Step("浏览器", False, "90 秒内没能就绪",
                              "机器太慢或者浏览器被安全软件挡了；"
                              "先跑一次 --selftest 看浏览器那一项"))
            return steps
        if hub.failed is not None:
            steps.append(Step("浏览器", False, str(hub.failed)[:200],
                              "多半是没装 Chromium："
                              "python -m playwright install chromium"))
            return steps
        steps.append(Step("浏览器", True, "已就绪（headless=%s），用时 %.1f 秒"
                          % (headless, time.time() - started)))

        # 等 WebSocket 挂上
        connected = channel.wait_connected(min(seconds, 60))
        if channel.blocked:
            steps.append(Step("风控", False, "被挡在验证页（%s）" % channel.blocked,
                              "加 --headful 重跑，在弹出的窗口里自己过一次验证；"
                              "配合 --user-data-dir 把验证结果留在本地"))
        else:
            steps.append(Step("风控", True, "没有落在验证页"))

        if not connected:
            steps.append(Step("弹幕连接", False,
                              "%d 秒内没挂上弹幕 WebSocket" % min(seconds, 60),
                              "页面打开了但直播间的 JS 没建立连接。"
                              "先看上面的「风控」那行；都正常的话开 --headful "
                              "看看页面实际长什么样"))
            return steps
        steps.append(Step("弹幕连接", True, "WebSocket 已挂上，用时 %.1f 秒"
                          % (time.time() - started)))

        # 收一段时间看看真有没有数据
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(0.5)

        if channel.frames == 0:
            steps.append(Step("收帧", False, "%d 秒内一帧都没收到" % seconds,
                              "连上了却没有数据。冷门房间确实可能很久没人说话，"
                              "换个热闹的直播间再试一次"))
            return steps
        steps.append(Step("收帧", True, "%d 秒收到 %d 帧" % (seconds, channel.frames)))

        total = sum(report.counts.values())
        if total == 0:
            steps.append(Step("解码", False,
                              "收到 %d 帧但一个事件都解不出来" % channel.frames,
                              "这是最值得上报的一种情况：抖音很可能改了 protobuf "
                              "的字段编号或消息结构。请把这份报告连同 --verbose "
                              "的日志一起贴到 issue 里"))
            return steps

        delay = (first_event[0] - started) if first_event else 0.0
        steps.append(Step("解码", True, "解出 %d 个事件（首个事件在 %.1f 秒时）"
                          % (total, delay)))

        chats = report.counts.get("chat", 0) + report.counts.get("emoji", 0)
        if chats == 0:
            others = "、".join(report.counts) or "无"
            steps.append(Step("聊天弹幕", False,
                              "有事件但没有一条聊天（收到的是 %s）" % others,
                              "进场和点赞能解出来、聊天解不出来的话，"
                              "多半是 ChatMessage 的字段编号变了"))
        else:
            steps.append(Step("聊天弹幕", True, "%d 条" % chats))
        return steps
    finally:
        channel.stop()
        hub.stop()


# --------------------------------------------------------------------------
# 串起来
# --------------------------------------------------------------------------

def run(target: str, seconds: int = DEFAULT_SECONDS, headless: bool = True,
        check_video: bool = True, user_data_dir: str = "", cookie: str = "",
        proxy: str = "") -> tuple[bool, str]:
    report = Report()
    sess = room.make_session(cookie)

    report.add(_step_network(sess))
    step, info = _step_room(target, sess)
    report.add(step)
    if info is None or not info.living:
        # 房间都没开播，后面的都没意义，但报告照样写出来
        return report.ok, report.render(target)

    if check_video:
        ffmpeg_step = report.add(_step_ffmpeg())
        if ffmpeg_step.ok:
            from . import video
            report.add(_step_stream(info, video.find_ffmpeg()))

    for step in _step_danmaku(info.web_rid or target, seconds, headless,
                              user_data_dir, cookie, proxy, report):
        report.steps.append(step)
        log.info("%s", step.render().splitlines()[0])

    return report.ok, report.render(target)


def _save(text: str) -> Optional[Path]:
    try:
        path = paths.app_data_dir() / "diagnose.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    p = argparse.ArgumentParser(
        prog="python -m dylive.diagnose",
        description="对着一个真实直播间，逐段确认录制链路到底断在哪一环。")
    p.add_argument("target", help="直播间号 / 直播间链接 / v.douyin.com 短链")
    p.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                   help="弹幕观察多久（默认 %d 秒）" % DEFAULT_SECONDS)
    p.add_argument("--headful", action="store_true",
                   help="显示浏览器窗口，被风控挡住时可以手动过验证")
    p.add_argument("--no-video", action="store_true", help="跳过 ffmpeg 和拉流那两段")
    p.add_argument("--user-data-dir", default="",
                   help="Chromium 用户目录，用来保留验证/登录结果")
    p.add_argument("--cookie", default="")
    p.add_argument("--proxy", default="", help="如 http://127.0.0.1:7890")
    p.add_argument("--verbose", action="store_true", help="打印 DEBUG 级别的日志")
    args = p.parse_args(argv)

    setup_logging("DEBUG" if args.verbose else "INFO")
    ok, text = run(args.target, seconds=args.seconds, headless=not args.headful,
                   check_video=not args.no_video,
                   user_data_dir=args.user_data_dir, cookie=args.cookie,
                   proxy=args.proxy)

    print()
    print(text)
    saved = _save(text)
    if saved:
        print("\n报告已保存到 %s" % saved)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
