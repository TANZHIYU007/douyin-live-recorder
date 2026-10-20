"""主播独立录制参数白名单；界面、持久化和回放共用，不能覆盖全局浏览器。"""

from __future__ import annotations

import math

from .messages import KIND_BY_METHOD

BOOL_FIELDS = {"record_video", "record_danmaku", "write_xml", "embed_subtitle",
               "subtitle_replace"}
ENUM_FIELDS = {"quality": {"origin", "FULL_HD1", "HD1", "SD1", "SD2"},
               "container": {"mp4", "flv", "ts", "mkv"},
               "subtitle_style": {"chat", "scroll"}}
NUMBER_FIELDS = {"segment_minutes": (0, 1440), "subtitle_size": (16, 120),
                 "subtitle_duration": (4, 30), "subtitle_reserve": (0, .8),
                 "subtitle_delay": (-30, 60)}
RECORDING_FIELDS = BOOL_FIELDS | ENUM_FIELDS.keys() | NUMBER_FIELDS.keys() | {"out_dir", "kinds"}


def clean_recording(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        if key in BOOL_FIELDS and isinstance(value, bool):
            clean[key] = value
        elif key in ENUM_FIELDS and isinstance(value, str) and value in ENUM_FIELDS[key]:
            clean[key] = value
        elif key in NUMBER_FIELDS and type(value) in (int, float) and math.isfinite(value):
            low, high = NUMBER_FIELDS[key]
            if low <= value <= high:
                clean[key] = int(value) if key in {"segment_minutes", "subtitle_size"} else float(value)
        elif key == "out_dir" and isinstance(value, str) and value.strip() and "\0" not in value:
            clean[key] = value.strip()
        elif key == "kinds" and isinstance(value, list):
            kinds = list(dict.fromkeys(k for k in value if isinstance(k, str)
                                      and k in KIND_BY_METHOD.values()))
            if kinds:
                clean[key] = kinds
    return clean


def clean_profiles(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    return {rid: clean for rid, value in raw.items()
            if isinstance(rid, str) and rid.isascii() and rid.isdigit()
            and (clean := clean_recording(value))}
