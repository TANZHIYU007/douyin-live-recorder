"""入口精简后，公开参数和默认录制行为必须保持兼容。"""

from pathlib import Path

import pytest

import main
from dylive.recorder import Options
from dylive.subtitle import Style


def options(*args):
    return Options.from_mapping(vars(main.build_parser().parse_args(["123", *args])))


def test_cli_defaults_match_recorder():
    assert options() == Options(target="123")


@pytest.mark.parametrize("flag,field,value", [
    ("--no-video", "record_video", False),
    ("--no-danmaku", "record_danmaku", False),
    ("--no-xml", "write_xml", False),
    ("--quiet-danmaku", "show_console", False),
    ("--headful", "headless", False),
    ("--no-block-media", "block_media", False),
    ("--embed", "embed_subtitle", True),
    ("--keep-original", "subtitle_replace", False),
    ("--watch", "watch", True),
])
def test_cli_switches(flag, field, value):
    assert options(flag) == Options(target="123", **{field: value})


def test_cli_named_values():
    got = options(
        "--out", "elsewhere", "--quality", "HD1", "--prefer", "hls",
        "--format", "mkv", "--segment", "60", "--ffmpeg", "ffmpeg-custom",
        "--engine", "native", "--user-data-dir", "profile", "--cookie", "test=1",
        "--proxy", "http://localhost:8080", "--subtitle-style", "scroll",
        "--subtitle-size", "36", "--subtitle-duration", "8",
        "--subtitle-reserve", "0.2", "--danmaku-delay", "-2.5",
        "--poll", "120", "--check", "45", "--retry", "15", "--kinds", "chat,gift")
    assert got == Options(
        target="123", out_dir=Path("elsewhere"), quality="HD1", prefer="hls",
        container="mkv", segment_seconds=60, ffmpeg="ffmpeg-custom", engine="native",
        user_data_dir="profile", cookie="test=1", proxy="http://localhost:8080",
        subtitle_style="scroll", subtitle_size=36, subtitle_duration=8,
        subtitle_reserve=0.2, subtitle_delay=-2.5, poll_interval=120,
        check_interval=45, retry_delay=15, kinds=("chat", "gift"))


def test_cli_all_kinds_and_invalid_kind():
    assert options("--kinds", "all").kinds == tuple(main.ALL_KINDS)
    with pytest.raises(SystemExit):
        options("--kinds", "unknown")


def test_mapping_filters_ui_fields_but_rejects_typo_in_override():
    assert Options.from_mapping({"target": "123", "theme": "dark"}, watch=True) == Options(
        target="123", watch=True)
    with pytest.raises(TypeError):
        Options.from_mapping({"target": "123"}, typo=True)


def test_auto_and_manual_subtitle_style_share_mapping():
    opts = Options(target="123", subtitle_style="scroll", subtitle_size=32,
                   subtitle_duration=6, subtitle_reserve=0.3, subtitle_delay=-1.5)
    assert Style.from_options(opts) == Style(mode="scroll", size=32, duration=6,
                                             reserve=0.3, delay=-1.5)
