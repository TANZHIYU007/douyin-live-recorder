#!/usr/bin/env python
"""拾光 Lumina —— 抖音直播录制命令行版（画面 + 弹幕）。

用法示例：
    python main.py 123456789                     # 录当前正在播的直播间
    python main.py https://live.douyin.com/xxx   # 直播间链接也行
    python main.py 123456789 --watch             # 没开播就守着，开播自动开录
    python main.py 123456789 --no-video          # 只收弹幕
    python main.py 123456789 --info              # 只看一眼房间状态
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from dylive import room, subtitle
from dylive.messages import DEFAULT_KINDS, KIND_BY_METHOD
from dylive.recorder import Options, Recorder
from dylive.utils import setup_console, setup_logging

ALL_KINDS = sorted(set(KIND_BY_METHOD.values()))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lumina",
        description="抖音直播录制：画面走 ffmpeg，弹幕走 WebSocket，时间轴对齐。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例：", 1)[-1],
    )
    p.add_argument("target", help="直播间号 / 直播间链接 / v.douyin.com 短链")
    p.add_argument("-o", "--out", dest="out_dir", type=Path, default=Path("output"),
                   help="输出目录（默认 output）")
    p.add_argument("--info", action="store_true", help="只打印直播间信息就退出")

    g = p.add_argument_group("录制内容")
    g.add_argument("--no-video", dest="record_video", action="store_false", help="不录画面，只收弹幕")
    g.add_argument("--no-danmaku", dest="record_danmaku", action="store_false", help="不收弹幕，只录画面")
    g.add_argument("--kinds", type=parse_kinds, default=DEFAULT_KINDS,
                   help="要记录的事件类型，逗号分隔；可选：%s；all 表示全要"
                        % ",".join(ALL_KINDS))
    g.add_argument("--no-xml", dest="write_xml", action="store_false", help="不生成 B 站格式 XML")
    g.add_argument("--quiet-danmaku", dest="show_console", action="store_false", help="不在终端实时打印弹幕")

    g = p.add_argument_group("画面")
    g.add_argument("-q", "--quality", default="origin",
                   help="画质：origin/蓝光/超清/高清/标清（默认 origin，拿不到会自动降级）")
    g.add_argument("--prefer", choices=["flv", "hls"], default="flv",
                   help="优先用哪种协议拉流（默认 flv，延迟更低）")
    g.add_argument("-f", "--format", dest="container",
                   choices=["mp4", "flv", "ts", "mkv"], default="mp4",
                   help="输出容器（默认 mp4，分片写入，中途断电也能播）")
    g.add_argument("--segment", dest="segment_seconds", type=int, default=0, metavar="秒",
                   help="按时长自动分片，0 表示不分（例如 3600 每小时一个文件）")
    g.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径")

    g = p.add_argument_group("弹幕引擎")
    g.add_argument("--engine", choices=["browser", "native"], default="browser",
                   help="browser=后台开 Chromium 旁听（默认，抗风控）；"
                        "native=直连 WebSocket（省资源，需要 assets/sign.js）")
    g.add_argument("--headful", dest="headless", action="store_false",
                   help="显示浏览器窗口，被风控挡住时可用它手动过验证")
    g.add_argument("--no-block-media", dest="block_media", action="store_false",
                   help="不拦截浏览器里的视频/图片（默认拦截以省资源）")
    g.add_argument("--user-data-dir", default="",
                   help="Chromium 用户目录，用来保留登录态")
    g.add_argument("--cookie", default="", help="自定义 Cookie 字符串")
    g.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")

    g = p.add_argument_group("弹幕字幕")
    g.add_argument("--embed", dest="embed_subtitle", action="store_true",
                   help="录完把弹幕做成 ASS 并作为软字幕封进视频（输出 mkv，不重编码）")
    g.add_argument("--keep-original", dest="subtitle_replace", action="store_false",
                   help="配合 --embed：封装成功后保留原始视频，默认校验通过就删掉")
    g.add_argument("--subtitle-style", choices=[subtitle.CHAT, subtitle.SCROLL],
                   default=subtitle.CHAT,
                   help="chat 左下角聊天流（默认，像直播间）/ scroll 滚动弹幕")
    g.add_argument("--subtitle-size", type=int, default=48, help="字幕字号（默认 48）")
    g.add_argument("--subtitle-duration", type=float, default=10.0, metavar="秒",
                   help="滚动弹幕横穿屏幕的秒数，聊天流不按时间消失（默认 10）")
    g.add_argument("--subtitle-reserve", type=float, default=0.4,
                   help="滚动样式：屏幕下方留白比例，避免挡住主播（默认 0.4）")
    g.add_argument("--danmaku-delay", dest="subtitle_delay", type=float, default=0.0, metavar="秒",
                   help="弹幕整体往后推的秒数，抵掉直播流比弹幕慢的那几秒。"
                        "字幕比画面早就调大，晚了填负数（默认 0）")

    g = p.add_argument_group("守候与重试")
    g.add_argument("-w", "--watch", action="store_true",
                   help="未开播时守候，开播自动开录，下播后继续守候")
    g.add_argument("--poll", dest="poll_interval", type=int, default=60, help="守候轮询间隔秒数（默认 60）")
    g.add_argument("--check", dest="check_interval", type=int, default=30,
                   help="录制中确认是否仍在播的间隔秒数（默认 30）")
    g.add_argument("--retry", dest="retry_delay", type=int, default=10, help="断流重试间隔秒数（默认 10）")

    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", type=Path, default=None, help="同时写一份日志到文件")
    return p


def parse_kinds(raw: str) -> tuple:
    if raw.strip().lower() == "all":
        return tuple(ALL_KINDS)
    kinds = tuple(k.strip() for k in raw.split(",") if k.strip())
    unknown = [k for k in kinds if k not in ALL_KINDS]
    if unknown:
        raise SystemExit("未知的事件类型：%s（可选：%s）"
                         % (",".join(unknown), ",".join(ALL_KINDS)))
    return kinds


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    log = logging.getLogger("main")

    if not args.record_video and not args.record_danmaku:
        log.error("--no-video 和 --no-danmaku 不能同时用，那样什么都不会录。")
        return 2

    if args.info:
        sess = room.make_session(args.cookie)
        info = room.fetch(room.parse_target(args.target, sess), sess)
        print(info.describe())
        for label, table in (("FLV", info.flv), ("HLS", info.hls)):
            for quality, url in table.items():
                print("  %-4s %-10s %s" % (label, quality, url))
        return 0 if info.living else 1

    opts = Options.from_mapping(vars(args))

    try:
        rec = Recorder(opts)
    except (ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 2

    # 第一次 Ctrl-C 走优雅收尾（让 ffmpeg 把文件写完），第二次才硬退
    def on_sigint(_sig, _frame):
        if rec._stop.is_set():
            log.warning("再次收到中断，强制退出")
            sys.exit(130)
        log.info("正在停止…（再按一次 Ctrl-C 强制退出）")
        rec.stop()

    signal.signal(signal.SIGINT, on_sigint)

    try:
        return rec.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
