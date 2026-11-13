"""语音字幕同时显示、保留原轨道与原子发布。"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from dylive import speech_subtitle, subtitle


def make_srt(path, text="这是主播说的话", end="00:00:02,500"):
    path.write_text("1\n00:00:00,500 --> %s\n%s\n" % (end, text), encoding="utf-8")
    return path


def test_combined_reserves_speech_area_and_uses_session_offset(tmp_path):
    srt = make_srt(tmp_path / "voice.srt", "语音{\\pos(0,0)}\n保留第二行")
    jsonl = tmp_path / "chat.jsonl"
    jsonl.write_text("\n".join(json.dumps({"kind": "chat", "offset": offset,
        "content": text}) for offset, text in [(9, "上片保留"), (10.5, "本片弹幕"),
                                               (13.1, "下一片不该出现")]), encoding="utf-8")
    out = tmp_path / "combined.ass"
    original_style = subtitle.Style(mode=subtitle.CHAT, size=18)
    assert speech_subtitle.build_combined_ass(srt, out, width=320, height=180,
        duration=3, jsonl=jsonl, offset=10, style=original_style) == 1
    text = out.read_text(encoding="utf-8-sig")
    assert "上片保留" in text and "本片弹幕" in text
    assert "下一片不该出现" not in text
    assert "0:00:00.50" in text
    assert "Style: Speech," in text and "Dialogue: 2," in text
    assert "语音｛＼pos(0,0)｝\\N保留第二行" in text
    assert original_style.margin_y == 0.08  # 不修改调用方保存的弹幕设置。
    # 动画的最高 y 起点也在语音字幕安全区之上。
    moves = re.findall(r"\\move\(\d+,(\d+),\d+,(\d+),", text)
    assert moves and max(int(value) for move in moves for value in move) < 150


def test_speech_text_is_split_without_dropping_content(tmp_path):
    content = "这是较长的一句话" * 12
    srt = make_srt(tmp_path / "voice.srt", content)
    out = tmp_path / "voice.ass"
    count = speech_subtitle.build_combined_ass(srt, out, width=320, height=180, duration=3)
    text = out.read_text(encoding="utf-8-sig")
    speech = [line.split(",", 9)[9] for line in text.splitlines()
              if line.startswith("Dialogue: 2,")]
    assert count > 1
    assert "".join(speech).replace("\\N", "") == content
    assert all(line.count("\\N") <= 1 for line in speech)


@pytest.mark.parametrize("body", ["not a subtitle", "1\n00:00:02,000 --> 00:00:01,000\nbad",
                                  "1\n00:00:10,000 --> 00:00:11,000\noutside"])
def test_invalid_or_empty_subtitle_fails_before_creating_media(tmp_path, body):
    path = tmp_path / "bad.srt"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(subtitle.SubtitleProcessError):
        speech_subtitle.build_combined_ass(path, tmp_path / "out.ass",
                                            width=320, height=180, duration=3)


def test_cancelled_task_does_not_touch_existing_files(tmp_path):
    video = tmp_path / "original.mp4"
    video.write_bytes(b"original")
    srt = make_srt(tmp_path / "voice.srt")
    with pytest.raises(speech_subtitle.SpeechSubtitleCancelled):
        speech_subtitle.mux_speech("ffmpeg", video, srt, cancelled=lambda: True)
    assert video.read_bytes() == b"original"
    assert not list(tmp_path.glob("*_语音字幕*.mkv"))


def test_explicit_existing_output_is_never_overwritten(tmp_path):
    video = tmp_path / "original.mp4"
    video.write_bytes(b"original")
    srt = make_srt(tmp_path / "voice.srt")
    out = tmp_path / "existing.mkv"
    out.write_bytes(b"user file")
    with pytest.raises(subtitle.SubtitleProcessError, match="已存在"):
        speech_subtitle.mux_speech("ffmpeg", video, srt, out=out)
    assert out.read_bytes() == b"user file"


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


@pytest.fixture()
def media(tmp_path):
    binary = os.environ.get("LUMINA_TEST_FFMPEG")
    if not binary:
        pytest.skip("设置 LUMINA_TEST_FFMPEG 启用真实字幕封装测试")
    ffmpeg = Path(binary)
    assert ffmpeg.is_file()
    raw = tmp_path / "original.mp4"
    run(ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=320x180:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440", "-f", "lavfi", "-i", "sine=frequency=880",
        "-t", "3", "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264",
        "-preset", "ultrafast", "-c:a", "aac", raw)
    return str(ffmpeg), raw


def inspect(ffmpeg, path):
    return json.loads(run(subtitle.find_ffprobe(ffmpeg), "-v", "error", "-show_streams",
                          "-show_format", "-of", "json", path))


def test_real_mux_preserves_all_audio_existing_subtitles_and_default_combined(media, tmp_path):
    ffmpeg, raw = media
    original = tmp_path / "original-with-danmaku.mkv"
    jsonl = tmp_path / "chat.jsonl"
    jsonl.write_text(json.dumps({"kind": "chat", "content": "原弹幕消息", "offset": 0.5}),
                     encoding="utf-8")
    ass = tmp_path / "old.ass"
    subtitle.build_ass(subtitle.load_events(jsonl), ass,
                       subtitle.Style(width=320, height=180, size=18), window=(0, 3))
    run(ffmpeg, "-v", "error", "-y", "-i", raw, "-i", ass, "-map", "0", "-map", "1",
        "-c", "copy", "-metadata:s:s:0", "title=原弹幕", "-disposition:s:0", "default", original)
    before = original.read_bytes()
    srt = make_srt(tmp_path / "voice.srt")
    out = speech_subtitle.mux_speech(ffmpeg, original, srt, jsonl=jsonl,
        style=subtitle.Style(mode=subtitle.CHAT, size=18), language="zh")
    assert original.read_bytes() == before
    info = inspect(ffmpeg, out)
    streams = info["streams"]
    assert len([s for s in streams if s["codec_type"] == "audio"]) == 2
    tracks = [s for s in streams if s["codec_type"] == "subtitle"]
    assert [s["tags"]["title"] for s in tracks] == ["原弹幕", "语音字幕", "弹幕字幕", "语音字幕 + 弹幕"]
    assert [s["disposition"]["default"] for s in tracks] == [0, 0, 0, 1]
    assert [s["codec_name"] for s in tracks] == ["ass", "subrip", "ass", "ass"]
    combined = run(ffmpeg, "-v", "error", "-i", out, "-map", "0:s:3", "-c:s", "ass",
                   "-f", "ass", "-").decode("utf-8")
    assert "这是主播说的话" in combined and "原弹幕消息" in combined
    assert "Style: Speech," in combined
    independent = run(ffmpeg, "-v", "error", "-i", out, "-map", "0:s:2", "-c:s", "ass",
                      "-f", "ass", "-").decode("utf-8")
    assert "原弹幕消息" in independent and "这是主播说的话" not in independent
    assert out.with_name(out.stem + "_弹幕.ass").is_file()
    run(ffmpeg, "-v", "error", "-i", out, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    again = speech_subtitle.mux_speech(ffmpeg, original, srt, jsonl=jsonl)
    assert out != again and again.name.endswith("_语音字幕_2.mkv")
    assert out.read_bytes()  # 先前输出未被替换。


def test_real_separate_tracks_work_for_raw_video_and_reserve_speech_area(media, tmp_path):
    ffmpeg, raw = media
    jsonl = tmp_path / "raw-chat.jsonl"
    jsonl.write_text(json.dumps({"kind": "chat", "content": "单独弹幕", "offset": 1}),
                     encoding="utf-8")
    srt = make_srt(tmp_path / "voice.srt")
    out = speech_subtitle.mux_speech(ffmpeg, raw, srt, jsonl=jsonl, combined=False,
                                    style=subtitle.Style(mode=subtitle.SCROLL, size=18))
    tracks = [s for s in inspect(ffmpeg, out)["streams"] if s["codec_type"] == "subtitle"]
    assert [s["tags"]["title"] for s in tracks] == ["语音字幕", "弹幕字幕"]
    assert [s["disposition"]["default"] for s in tracks] == [1, 0]
    ass = out.with_name(out.stem + "_弹幕.ass").read_text(encoding="utf-8-sig")
    assert "单独弹幕" in ass and "这是主播说的话" not in ass
    # 滚动弹幕位于顶部，语音字幕位于底部中央。
    assert "\\move(320,0," in ass


def test_real_no_jsonl_cannot_silently_drop_existing_danmaku(media, tmp_path):
    ffmpeg, raw = media
    srt = make_srt(tmp_path / "voice.srt")
    embedded = tmp_path / "with-voice.mkv"
    run(ffmpeg, "-v", "error", "-y", "-i", raw, "-i", srt,
        "-map", "0", "-map", "1", "-c", "copy", embedded)
    with pytest.raises(subtitle.SubtitleProcessError, match="缺少本场弹幕 JSONL"):
        speech_subtitle.mux_speech(ffmpeg, embedded, srt, combined=True)
    out = speech_subtitle.mux_speech(ffmpeg, embedded, srt, combined=False)
    assert len([s for s in inspect(ffmpeg, out)["streams"] if s["codec_type"] == "subtitle"]) == 2


def test_real_mov_text_and_long_cues_remain_compatible_with_matroska(media, tmp_path):
    ffmpeg, raw = media
    old = make_srt(tmp_path / "old.srt", "既有字幕")
    embedded = tmp_path / "movtext.mp4"
    run(ffmpeg, "-v", "error", "-y", "-i", raw, "-i", old, "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "mov_text", embedded)
    srt = make_srt(tmp_path / "voice.srt", end="00:00:20,000")
    out = speech_subtitle.mux_speech(ffmpeg, embedded, srt, combined=False)
    info = inspect(ffmpeg, out)
    assert len([s for s in info["streams"] if s["codec_type"] == "subtitle"]) == 2
    assert all(s["codec_name"] == "subrip" for s in info["streams"] if s["codec_type"] == "subtitle")
    assert abs(float(info["format"]["duration"]) - 3) < 0.2
