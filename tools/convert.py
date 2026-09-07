#!/usr/bin/env python
"""把拾光录下来的 .jsonl 弹幕转成别的格式，或补做带弹幕的视频。

    python tools/convert.py 某场录像.jsonl --ass            # 生成 ASS 字幕
    python tools/convert.py 某场录像.jsonl --xml --txt
    python tools/convert.py 某场录像.jsonl --embed          # 封装进同名视频（输出 mkv）

ASS 的排版逻辑在 dylive/subtitle.py，和界面里自动生成的那份是同一套实现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dylive import subtitle, video                          # noqa: E402
from dylive.utils import setup_console, setup_logging       # noqa: E402


def to_xml(events, out: Path) -> int:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<i>\n'
             '  <chatserver>live.douyin.com</chatserver>\n'
             '  <chatid>0</chatid><mission>0</mission>\n'
             '  <maxlimit>2147483647</maxlimit><state>0</state>\n'
             '  <source>Lumina</source>\n']
    for ev in events:
        attr = "%.3f,1,25,16777215,%d,0,%s,%s" % (
            max(0.0, float(ev.get("offset", 0))), int(ev.get("ts", 0)),
            ev.get("user_id") or "0", (ev.get("extra") or {}).get("msg_id", "0"))
        parts.append('  <d p="%s">%s</d>\n' % (attr, escape(ev["content"])))
    parts.append("</i>\n")
    out.write_text("".join(parts), encoding="utf-8")
    return len(parts) - 2


def to_txt(events, out: Path) -> int:
    with out.open("w", encoding="utf-8") as fh:
        for ev in events:
            sec = int(max(0.0, float(ev.get("offset", 0))))
            fh.write("[%02d:%02d:%02d] %s: %s\n"
                     % (sec // 3600, sec % 3600 // 60, sec % 60,
                        ev.get("user_name") or "-", ev["content"]))
    return len(events)


def main() -> int:
    setup_console()
    setup_logging("INFO")
    p = argparse.ArgumentParser(description="把拾光录下的 jsonl 弹幕转成其他格式")
    p.add_argument("jsonl", type=Path)
    p.add_argument("--ass", action="store_true", help="生成 ASS 滚动弹幕字幕")
    p.add_argument("--xml", action="store_true", help="生成 B 站格式 XML")
    p.add_argument("--txt", action="store_true", help="生成带时间戳的纯文本")
    p.add_argument("--embed", action="store_true",
                   help="把弹幕作为软字幕封进同名视频，输出 mkv（不重编码）")
    p.add_argument("--replace", action="store_true",
                   help="配合 --embed：封装成功后删掉原视频")
    p.add_argument("--kinds", default="chat,emoji",
                   help="参与转换的事件类型，逗号分隔（默认 chat,emoji）")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--font", default="", help="字幕字体，留空按当前系统挑")
    p.add_argument("--size", type=int, default=48)
    p.add_argument("--duration", type=float, default=10.0,
                   help="一条弹幕横穿屏幕的秒数（默认 10）")
    p.add_argument("--opacity", type=float, default=0.85, help="不透明度 0~1")
    p.add_argument("--style", choices=[subtitle.SCROLL, subtitle.CHAT],
                   default=subtitle.CHAT,
                   help="chat 左下角聊天流（默认，像直播间）/ scroll 滚动弹幕")
    p.add_argument("--lines", type=int, default=8,
                   help="chat 样式：左下角最多同时显示几条（默认 8）")
    p.add_argument("--rate", type=float, default=2.5,
                   help="chat 样式：每秒最多显示几条，超了丢弃（默认 2.5）")
    p.add_argument("--reserve", type=float, default=0.4,
                   help="scroll 样式：屏幕下方留白比例，避免挡字幕（默认 0.4）")
    p.add_argument("--ffmpeg", default="", help="ffmpeg 路径（--embed 时用）")
    args = p.parse_args()

    if not args.jsonl.exists():
        print("找不到文件：%s" % args.jsonl)
        return 2
    if not (args.ass or args.xml or args.txt or args.embed):
        args.ass = True

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    events = subtitle.load_events(args.jsonl, kinds)
    if not events:
        print("没有匹配的弹幕（--kinds %s）" % args.kinds)
        return 1
    print("读入 %d 条弹幕" % len(events))

    style = subtitle.Style(width=args.width, height=args.height, font=args.font,
                           size=args.size, duration=args.duration,
                           opacity=args.opacity, reserve=args.reserve,
                           mode=args.style, lines=args.lines, max_rate=args.rate)
    base = args.jsonl.with_suffix("")

    if args.ass:
        n = subtitle.build_ass(events, base.with_suffix(".ass"), style)
        print("  -> %s（%d 条）" % (base.with_suffix(".ass").name, n))
    if args.xml:
        n = to_xml(events, base.with_suffix(".xml"))
        print("  -> %s（%d 条）" % (base.with_suffix(".xml").name, n))
    if args.txt:
        n = to_txt(events, base.with_suffix(".txt"))
        print("  -> %s（%d 条）" % (base.with_suffix(".txt").name, n))

    if args.embed:
        try:
            ffmpeg = video.find_ffmpeg(args.ffmpeg)
        except RuntimeError as exc:
            print(exc)
            return 2
        videos = subtitle.find_videos(base)
        if not videos:
            print("同目录下没找到 %s* 的视频文件" % base.name)
            return 1
        print("找到 %d 个视频片段，开始封装…" % len(videos))
        results = subtitle.process(ffmpeg, args.jsonl, videos, style=style,
                                   kinds=kinds, replace=args.replace,
                                   progress=lambda m: print("  " + m))
        for r in results:
            print("  -> %s" % r.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
