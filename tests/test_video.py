"""ffmpeg 命令拼装与产出文件的识别。

不跑 ffmpeg，只检查命令行长什么样、文件名怎么算 —— 这两件事错了，
要么录不出来，要么录完了找不到。
"""

import time
from pathlib import Path

import pytest

from dylive import video
from dylive.video import VideoRecorder


def rec(url="http://x/live.flv", out="录像", container="mp4", **kw) -> VideoRecorder:
    return VideoRecorder("ffmpeg", url, Path(out), container=container, **kw)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def test_只做_remux_不转码():
    cmd = rec().build_command()
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"


def test_带上断线重连和读超时():
    """ffmpeg 自己先扛一层短暂抖动，扛不住再交给外层重试。"""
    cmd = rec().build_command()
    assert "-reconnect" in cmd and "-reconnect_streamed" in cmd
    assert "-rw_timeout" in cmd


def test_HLS_进_mp4_要转_ADTS_头():
    """HLS 的音频是 ADTS 帧，不转成 ASC 头塞进 mp4 就是坏的。"""
    cmd = rec(url="http://x/live.m3u8").build_command()
    assert "-bsf:a" in cmd and cmd[cmd.index("-bsf:a") + 1] == "aac_adtstoasc"


def test_FLV_源不需要转():
    assert "-bsf:a" not in rec(url="http://x/live.flv").build_command()


def test_HLS_进_ts_也不需要转():
    assert "-bsf:a" not in rec(url="http://x/live.m3u8", container="ts").build_command()


def test_mp4_用分片写入():
    """中途断电时，已经写下的部分照样能播。"""
    cmd = rec().build_command()
    assert "+frag_keyframe+empty_moov+default_base_moof" in " ".join(cmd)


def test_分片模式():
    r = rec(segment_seconds=3600)
    cmd = r.build_command()
    assert "-f" in cmd and "segment" in cmd
    assert "3600" in cmd
    assert r.out.name == "录像_part%03d.mp4"


def test_不分片时就是一个文件():
    assert rec().out.name == "录像.mp4"


def test_不支持的容器直接报错():
    with pytest.raises(ValueError):
        rec(container="avi")


@pytest.mark.parametrize("container,suffix", [
    ("mp4", ".mp4"), ("flv", ".flv"), ("ts", ".ts"), ("mkv", ".mkv")])
def test_各容器的扩展名(container, suffix):
    assert rec(container=container).out.suffix == suffix


def test_额外参数接在最后():
    cmd = rec(extra_args=["-t", "60"]).build_command()
    assert cmd[-3:] == ["-t", "60", str(rec().out)]


# --------------------------------------------------------------------------
# 时间零点
# --------------------------------------------------------------------------

def test_没写出数据时零点退回启动时刻(tmp_path):
    r = rec(out=tmp_path / "a")
    r.started_at = 1000.0
    assert r.zero_at() == 1000.0


def test_note_first_data_记下真正开始写盘的时刻(tmp_path):
    """起录时刻不能用「进程起来了」那一刻 —— ffmpeg 还要连 CDN、等关键帧。"""
    r = rec(out=tmp_path / "a")
    r.started_at = 1000.0

    assert r.note_first_data() is False          # 文件还不存在
    r.out.write_bytes(b"")
    assert r.note_first_data() is False          # 文件在但是空的
    assert r.zero_at() == 1000.0

    before = time.time()
    r.out.write_bytes(b"\x00" * 16)
    assert r.note_first_data() is True
    assert r.first_data_at >= before
    assert r.zero_at() == r.first_data_at


def test_零点只记一次(tmp_path):
    r = rec(out=tmp_path / "a")
    r.out.write_bytes(b"x")
    r.note_first_data()
    first = r.first_data_at
    r.note_first_data()
    assert r.first_data_at == first


# --------------------------------------------------------------------------
# 找产出文件
# --------------------------------------------------------------------------

def test_files_按前缀匹配而不是_glob(tmp_path):
    """直播间标题里的 [ ] 是 glob 元字符，用 glob 会匹配出莫名其妙的结果。"""
    r = rec(out=tmp_path / "20260925_标题[直播]")
    r.out.write_bytes(b"x")
    (tmp_path / "别的.mp4").write_bytes(b"x")

    assert [p.name for p in r.files()] == ["20260925_标题[直播].mp4"]


def test_分片模式按编号排序(tmp_path):
    r = rec(out=tmp_path / "录像", segment_seconds=60)
    for i in (2, 0, 1):
        (tmp_path / ("录像_part%03d.mp4" % i)).write_bytes(b"x")
    (tmp_path / "录像_part000.jsonl").write_bytes(b"x")    # 扩展名不对，不算

    assert [p.name for p in r.files()] == [
        "录像_part000.mp4", "录像_part001.mp4", "录像_part002.mp4"]


def test_文件还没生成时返回空(tmp_path):
    assert rec(out=tmp_path / "还没有").files() == []


def test_容器表覆盖了界面里能选的四种():
    assert set(video.CONTAINERS) == {"mp4", "flv", "ts", "mkv"}
