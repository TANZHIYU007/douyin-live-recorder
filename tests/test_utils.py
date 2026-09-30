"""文件名清洗、时间格式化、弹幕落盘和日志。

直播间标题什么字符都可能有，落盘之前必须洗干净 —— 洗不干净就是整场录不下来。
"""

import json
import logging

import pytest

from dylive import utils
from dylive.messages import Event
from dylive.writers import JsonlWriter, Writer, WriterGroup, XmlWriter


# --------------------------------------------------------------------------
# 文件名
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("正常标题", "正常标题"),
    ("带/斜杠", "带_斜杠"),
    # 连着几个非法字符合成一个下划线；结尾的下划线会被 strip 掉
    ("冒号:星号*问号?", "冒号_星号_问号"),
    ('引号"尖括号<>', "引号_尖括号"),
    ("竖线|反斜杠\\斜杠/", "竖线_反斜杠_斜杠"),
    # 反斜杠在 Windows 上是路径分隔符，留着录像会落进一层子目录
    ("主播\\标题", "主播_标题"),
    ("换\n行\t制表", "换_行_制表"),
    ("  前后空格  ", "前后空格"),
    ("多个    空格", "多个 空格"),
    ("...前后点...", "前后点"),
])
def test_safe_name(raw, expect):
    assert utils.safe_name(raw) == expect


def test_safe_name_截断到上限():
    assert len(utils.safe_name("字" * 200, 40)) == 40


@pytest.mark.parametrize("raw", ["", "   ", "...", "___", None])
def test_safe_name_洗空了给个兜底名(raw):
    assert utils.safe_name(raw) == "untitled"


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expect", [
    (0, "00:00:00.00"),
    (1.5, "00:00:01.50"),
    (61, "00:01:01.00"),
    (3661, "01:01:01.00"),
    (-5, "00:00:00.00"),            # 负数贴到 0
])
def test_hms(seconds, expect):
    assert utils.hms(seconds) == expect


@pytest.mark.parametrize("seconds,expect", [
    (-1, "00:00:00"), (1.99, "00:00:01"), (3661, "01:01:01"), (360000, "100:00:00"),
])
def test_shared_clock(seconds, expect):
    assert utils.clock(seconds) == expect


@pytest.mark.parametrize("size,expect", [
    (0, "0.0 MB"), (1 << 20, "1.0 MB"), (1 << 30, "1.00 GB"),
])
def test_shared_file_size(size, expect):
    assert utils.human_size(size) == expect


def test_stamp_是可以当文件名的():
    s = utils.stamp(0)
    assert len(s) == 15 and s[8] == "_"
    assert utils.safe_name(s) == s


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def event(kind="chat", content="你好", offset=1.5) -> Event:
    return Event(ts=1700000000.0, offset=offset, kind=kind,
                 method="WebcastChatMessage", user_id="88",
                 user_name="路过的风", content=content)


def test_jsonl_一行一条(tmp_path):
    path = tmp_path / "a.jsonl"
    w = JsonlWriter(path)
    w.write(event())
    w.write(event(content="第二条"))
    w.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"content": "你好"' in lines[0] or '"content":"你好"' in lines[0]


def test_jsonl_不带_extra_时省掉那个字段(tmp_path):
    path = tmp_path / "a.jsonl"
    w = JsonlWriter(path)
    w.write(event())
    w.close()
    assert "extra" not in json.loads(path.read_text(encoding="utf-8"))


def test_xml_收尾写上闭合标签(tmp_path):
    path = tmp_path / "a.xml"
    w = XmlWriter(path, room="123")
    w.write(event())
    w.write(event(kind="gift", content="送出礼物"))   # 只有聊天进 XML
    w.close()

    text = path.read_text(encoding="utf-8")
    assert text.rstrip().endswith("</i>")
    assert text.count("<d p=") == 1


def test_xml_转义掉尖括号(tmp_path):
    path = tmp_path / "a.xml"
    w = XmlWriter(path)
    w.write(event(content="<script>&"))
    w.close()
    text = path.read_text(encoding="utf-8")
    assert "&lt;script&gt;&amp;" in text


def test_writer_group_里一个坏了不影响别的(tmp_path):
    class 会炸的(Writer):
        def write(self, ev):
            raise RuntimeError("炸了")

    class 计数的(Writer):
        def __init__(self):
            self.n = 0

        def write(self, ev):
            self.n += 1

    good = 计数的()
    group = WriterGroup([会炸的(), good])
    group.write(event())
    group.write(event())

    assert good.n == 2
    assert group.total == 2


# --------------------------------------------------------------------------
# 日志落盘
# --------------------------------------------------------------------------

@pytest.fixture
def clean_root_logger():
    """用完把根 logger 恢复原样，免得污染别的用例。"""
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield root
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(before_level)


def test_日志真的写进文件(tmp_path, monkeypatch, clean_root_logger):
    monkeypatch.setattr(utils, "log_dir", lambda: tmp_path / "logs")
    clean_root_logger.setLevel(logging.DEBUG)

    path = utils.add_rotating_file_log()
    assert path is not None and path.parent == tmp_path / "logs"

    logging.getLogger("测试").info("录制开始")
    for h in clean_root_logger.handlers:
        h.flush()
    assert "录制开始" in path.read_text(encoding="utf-8")


def test_默认不记_DEBUG_打开详细日志才记(tmp_path, monkeypatch, clean_root_logger):
    monkeypatch.setattr(utils, "log_dir", lambda: tmp_path / "logs")
    clean_root_logger.setLevel(logging.DEBUG)

    path = utils.add_rotating_file_log(verbose=False)
    logging.getLogger("测试").debug("握手细节")
    for h in clean_root_logger.handlers:
        h.flush()
    assert "握手细节" not in path.read_text(encoding="utf-8")

    utils.add_rotating_file_log(verbose=True)          # 改级别，不新挂一份
    logging.getLogger("测试").debug("握手细节")
    for h in clean_root_logger.handlers:
        h.flush()
    assert "握手细节" in path.read_text(encoding="utf-8")


def test_重复调用不会挂出两份处理器(tmp_path, monkeypatch, clean_root_logger):
    monkeypatch.setattr(utils, "log_dir", lambda: tmp_path / "logs")
    before = len(clean_root_logger.handlers)

    utils.add_rotating_file_log()
    utils.add_rotating_file_log()
    utils.add_rotating_file_log(verbose=True)

    assert len(clean_root_logger.handlers) == before + 1


def test_目录建不起来时不崩只是没有文件日志(tmp_path, monkeypatch, clean_root_logger):
    blocker = tmp_path / "被占了"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(utils, "log_dir", lambda: blocker / "logs")

    assert utils.add_rotating_file_log() is None


def test_压掉吵闹的第三方日志(clean_root_logger):
    utils.quiet_noisy_loggers()
    assert logging.getLogger("websocket").level == logging.CRITICAL
    assert logging.getLogger("urllib3").level == logging.WARNING
