"""弹幕 -> ASS。

重点有三块：分段平移（断流重连产生多个文件）、延迟补偿（弹幕比画面早到）、
以及限流（ffmpeg 的 ass 编码器扛不住太多 Dialogue）。
"""

import json
import re

import pytest

from dylive import subtitle
from dylive.subtitle import Style


def ev(offset: float, content: str = "内容", user: str = "路过的风",
       kind: str = "chat") -> dict:
    return {"offset": offset, "content": content, "user_name": user, "kind": kind}


def starts(text: str):
    """从 ASS 文本里取出每条 Dialogue 的开始时间（秒）。"""
    out = []
    for h, m, s in re.findall(r"Dialogue: \d+,(\d+):(\d\d):(\d\d\.\d\d),", text):
        out.append(int(h) * 3600 + int(m) * 60 + float(s))
    return out


# --------------------------------------------------------------------------
# 读 jsonl
# --------------------------------------------------------------------------

def test_load_events_跳过坏行(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text(
        json.dumps({"kind": "chat", "content": "好", "offset": 1}) + "\n"
        + "{这行是断电留下的半条\n"
        + "\n"
        + json.dumps({"kind": "gift", "content": "礼物", "offset": 2}) + "\n"
        + json.dumps({"kind": "chat", "content": "", "offset": 3}) + "\n",
        encoding="utf-8")

    got = subtitle.load_events(path, ("chat", "gift"))
    assert [e["content"] for e in got] == ["好", "礼物"]     # 空内容的那条也被滤掉


def test_load_events_文件不存在不抛(tmp_path):
    assert subtitle.load_events(tmp_path / "没有.jsonl") == []


# --------------------------------------------------------------------------
# 延迟补偿
# --------------------------------------------------------------------------

def test_延迟补偿把弹幕整体往后推(tmp_path):
    out = tmp_path / "a.ass"
    subtitle.build_ass([ev(10.0)], out, Style(mode=subtitle.SCROLL, delay=5.0))
    assert starts(out.read_text(encoding="utf-8-sig")) == [15.0]


def test_不补偿时保持原样(tmp_path):
    out = tmp_path / "a.ass"
    subtitle.build_ass([ev(10.0)], out, Style(mode=subtitle.SCROLL))
    assert starts(out.read_text(encoding="utf-8-sig")) == [10.0]


def test_负的补偿把弹幕往前提(tmp_path):
    out = tmp_path / "a.ass"
    subtitle.build_ass([ev(10.0)], out, Style(mode=subtitle.SCROLL, delay=-4.0))
    assert starts(out.read_text(encoding="utf-8-sig")) == [6.0]


def test_补偿后不会出现负时间(tmp_path):
    """推到 0 之前的一律贴到 0，ASS 里没有负时间这回事。"""
    out = tmp_path / "a.ass"
    subtitle.build_ass([ev(1.0)], out, Style(mode=subtitle.SCROLL, delay=-10.0))
    assert starts(out.read_text(encoding="utf-8-sig")) == [0.0]


def test_补偿要在分段之前生效(tmp_path):
    """推后 5 秒之后，本来卡在第一段末尾的弹幕应该落到第二段开头。

    顺序反了的话第二段开头会缺一截 —— 这是最容易写反的一处。
    """
    events = [ev(97.0, "第一段末尾"), ev(103.0, "第二段")]
    st = Style(mode=subtitle.SCROLL, delay=5.0)

    first = tmp_path / "1.ass"
    n1 = subtitle.build_ass(events, first, st, shift=0.0, window=(0.0, 100.0))
    second = tmp_path / "2.ass"
    n2 = subtitle.build_ass(events, second, st, shift=100.0, window=(100.0, 200.0))

    assert n1 == 0              # 97 + 5 = 102，已经不属于第一段了
    assert n2 == 2              # 两条都落到第二段
    assert starts(second.read_text(encoding="utf-8-sig")) == [2.0, 8.0]


def test_offset_坏掉的那条被跳过而不是整场失败(tmp_path):
    out = tmp_path / "a.ass"
    events = [{"offset": "不是数字", "content": "坏的", "user_name": "x"},
              ev(5.0, "好的")]
    assert subtitle.build_ass(events, out, Style(mode=subtitle.SCROLL)) == 1


# --------------------------------------------------------------------------
# 分段平移
# --------------------------------------------------------------------------

def test_第二段按自己的起点重新计时(tmp_path):
    events = [ev(10.0, "第一段"), ev(130.0, "第二段")]
    out = tmp_path / "b.ass"
    n = subtitle.build_ass(events, out, Style(mode=subtitle.SCROLL),
                           shift=120.0, window=(120.0, 240.0))
    assert n == 1
    assert starts(out.read_text(encoding="utf-8-sig")) == [10.0]


def test_窗口之外的弹幕不进这一段(tmp_path):
    events = [ev(t) for t in (5.0, 50.0, 500.0)]
    out = tmp_path / "c.ass"
    n = subtitle.build_ass(events, out, Style(mode=subtitle.SCROLL),
                           shift=0.0, window=(0.0, 100.0))
    assert n == 2


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------

def test_滚动样式每条一行(tmp_path):
    out = tmp_path / "d.ass"
    n = subtitle.build_ass([ev(1.0), ev(2.0), ev(3.0)], out,
                           Style(mode=subtitle.SCROLL))
    text = out.read_text(encoding="utf-8-sig")
    assert n == 3
    assert text.count("Dialogue:") == 3
    assert "\\move(" in text


def test_聊天流样式底框和文字各一层(tmp_path):
    """底框不分色段才只画一个气泡，所以必须是成对的两条 Dialogue。"""
    out = tmp_path / "e.ass"
    subtitle.build_ass([ev(1.0), ev(3.0)], out, Style(mode=subtitle.CHAT))
    text = out.read_text(encoding="utf-8-sig")

    layer0 = text.count("Dialogue: 0,")
    layer1 = text.count("Dialogue: 1,")
    assert layer0 == layer1 and layer0 > 0
    assert "\\1a&HFF&" in text           # 底框那条的文字是全透明的
    assert "Style: DM," in text and "Style: DMT," in text


def test_聊天流的底框那条不带颜色标签(tmp_path):
    out = tmp_path / "f.ass"
    subtitle.build_ass([ev(1.0, "你好", "路过的风")], out, Style(mode=subtitle.CHAT))
    for line in out.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue: 0,"):
            assert "\\c&H" not in line   # 分了色段就会画出两个框，接缝处发深
        if line.startswith("Dialogue: 1,"):
            assert "\\c&H" in line


def test_没有弹幕时返回零(tmp_path):
    out = tmp_path / "g.ass"
    assert subtitle.build_ass([], out, Style()) == 0


def test_画面尺寸写进_ASS_头(tmp_path):
    out = tmp_path / "h.ass"
    subtitle.build_ass([ev(1.0)], out, Style(width=1080, height=1920))
    text = out.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text


def test_花括号和反斜杠被转义掉(tmp_path):
    """不转义的话 {\\an8} 这种内容会被 libass 当成标签执行。"""
    out = tmp_path / "i.ass"
    subtitle.build_ass([ev(1.0, "{\\an8}假标签")], out, Style(mode=subtitle.SCROLL))
    body = [ln for ln in out.read_text(encoding="utf-8-sig").splitlines()
            if ln.startswith("Dialogue:")][0]
    tail = body.split(",,", 1)[1]
    assert "{\\an8}" not in tail
    assert "｛" in tail and "＼" in tail


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------

def test_按最小间隔限流():
    picked = [(i * 0.1, ev(i * 0.1)) for i in range(100)]      # 每秒 10 条
    kept = subtitle._thin(picked, Style(max_rate=2.0))
    assert len(kept) == pytest.approx(20, abs=2)


def test_总量超上限时整体抽稀():
    picked = [(i * 10.0, ev(i * 10.0)) for i in range(subtitle.MAX_DIALOGUE)]
    kept = subtitle._thin(picked, Style(max_rate=1.0))
    assert len(kept) <= subtitle.MAX_DIALOGUE // 2
    assert kept[0][0] == 0.0                    # 首尾都还在，只是中间稀了


def test_截断按预算走并加省略号():
    clipped = subtitle._clip("一二三四五六七八九十", 48, 48 * 4)
    assert clipped.endswith("…")
    assert len(clipped) < 10


def test_预算再小也要截():
    assert subtitle._clip("一二三", 48, 1) == "一…"


def test_中文按一个字宽半角按半个():
    assert subtitle._text_width("中文", 10) == 20.0
    assert subtitle._text_width("ab", 10) == 10.0


def test_颜色从_RRGGBB_转成_ASS_的_BBGGRR():
    assert subtitle._bgr("A9D3FF") == "FFD3A9"
    assert subtitle._bgr("#A9D3FF") == "FFD3A9"
    assert subtitle._bgr("坏的") == "FFD3A9"        # 回落到默认色


# --------------------------------------------------------------------------
# 找视频文件
# --------------------------------------------------------------------------

def test_find_videos_按前缀匹配且排除已生成的弹幕版(tmp_path):
    base = tmp_path / "20260925_2041_标题[直播]"
    for name in (".mp4", "_r1.mp4", "_弹幕版.mkv", ".jsonl", ".xml"):
        (tmp_path / (base.name + name)).write_bytes(b"x")
    (tmp_path / "别人的录像.mp4").write_bytes(b"x")

    got = [p.name for p in subtitle.find_videos(base)]
    assert got == [base.name + ".mp4", base.name + "_r1.mp4"]


def test_find_videos_目录不存在不抛(tmp_path):
    assert subtitle.find_videos(tmp_path / "没有" / "前缀") == []
