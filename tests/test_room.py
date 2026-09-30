"""直播间信息解析。

最要紧的一条是 ok 和 living 分得开：断网时 status 也是 0，跟「确认未开播」
长得一模一样，混在一起就会在最不该停的时候收工。
"""

import json

import pytest

from dylive import room
from dylive.room import RoomInfo


# --------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target,expect", [
    ("123456789", "123456789"),
    ("  123456789  ", "123456789"),
    ("https://live.douyin.com/123456789", "123456789"),
    ("https://live.douyin.com/123456789?from=share", "123456789"),
    ("live.douyin.com/123456789/", "123456789"),
    ("https://www.douyin.com/123456789", "123456789"),
    ("https://live.douyin.com/?live_web_rid=214495728391", "214495728391"),
    ("https://live.douyin.com/?from=share&live_web_rid=214495728391", "214495728391"),
    ("https://www.douyin.com/follow?web_rid=987654321", "987654321"),
    ("https://live.douyin.com/user/xxx?web_rid=555666777", "555666777"),
])
def test_parse_target(target, expect):
    assert room.parse_target(target) == expect


@pytest.mark.parametrize("junk", [
    "", "抖音", "https://example.com/", "abc",
    "https://example.com/?live_web_rid=214495728391",
    "https://live.douyin.com.evil.example/214495728391",
    "https://live.douyin.com/?live_web_rid=not-a-room",
])
def test_parse_target_解析不出来就报错(junk):
    with pytest.raises(ValueError):
        room.parse_target(junk)


def test_parse_target_短链跳转后识别新版参数():
    class Session:
        def get(self, url, **kwargs):
            assert url == "https://v.douyin.com/short/"
            assert kwargs["allow_redirects"] is True
            return type("Response", (), {
                "url": "https://live.douyin.com/?live_web_rid=214495728391"
            })()

    assert room.parse_target("v.douyin.com/short/", Session()) == "214495728391"


# --------------------------------------------------------------------------
# living / ok
# --------------------------------------------------------------------------

def test_查询失败和未开播是两回事():
    """断网（ok=False）绝不能和「主播下播了」混为一谈。"""
    broken = RoomInfo(web_rid="1", ok=False)
    offline = RoomInfo(web_rid="1", ok=True, status=4)

    assert broken.living is False and offline.living is False
    assert broken.ok is False and offline.ok is True
    assert "查询失败" in broken.describe()
    assert "未开播" in offline.describe()


def test_状态是直播中但没有地址不算在播():
    """拿不到拉流地址的话，说「在播」对调用方没有意义。"""
    assert RoomInfo(web_rid="1", ok=True, status=2).living is False
    assert RoomInfo(web_rid="1", ok=True, status=2,
                    flv={"origin": "http://x/1.flv"}).living is True


# --------------------------------------------------------------------------
# 画质挑选
# --------------------------------------------------------------------------

def test_指定画质拿得到就用它():
    info = RoomInfo(web_rid="1", flv={"origin": "o", "HD1": "h", "SD2": "s"})
    assert info.pick("origin") == "o"
    assert info.pick("超清") == "h"
    assert info.pick("标清") == "s"


def test_指定画质拿不到就顺次降级():
    info = RoomInfo(web_rid="1", flv={"SD1": "sd1", "SD2": "sd2"})
    assert info.pick("origin") == "sd1"          # origin -> ... -> SD1


def test_低画质拿不到时会回头找高的():
    """降级列表兜了一圈：SD2 没有就从头找。

    预览想要「只要最低画质」，靠 pick 是保证不了的 —— 房间没有标清时它会
    一路回落到原画。这条用例把这个行为钉住，免得以后误以为 pick 能做限流。
    """
    info = RoomInfo(web_rid="1", flv={"origin": "o"})
    assert info.pick("标清") == "o"


def test_prefer_决定先看_flv_还是_hls():
    info = RoomInfo(web_rid="1", flv={"origin": "f"}, hls={"origin": "h"})
    assert info.pick("origin", "flv") == "f"
    assert info.pick("origin", "hls") == "h"


def test_首选协议没有时用另一个():
    info = RoomInfo(web_rid="1", hls={"origin": "h"})
    assert info.pick("origin", "flv") == "h"


def test_一个地址都没有返回_None():
    assert RoomInfo(web_rid="1").pick() is None


# --------------------------------------------------------------------------
# enter 接口
# --------------------------------------------------------------------------

def enter_payload(status: int = 2) -> dict:
    stream_data = json.dumps({"data": {
        "origin": {"main": {"flv": "http://x/origin.flv", "hls": "http://x/origin.m3u8"}},
    }})
    return {"data": {
        "web_rid": "123456789",
        "user": {"nickname": "夜航西飞",
                 "avatar_thumb": {"url_list": ["http://x/avatar.jpg"]}},
        "data": [{
            "id_str": "7300000000000000001",
            "title": "深夜飞行",
            "status": status,
            "user_count_str": "1.2万",
            "cover": {"url_list": ["http://x/cover.jpg"]},
            "stream_url": {
                "flv_pull_url": {"FULL_HD1": "http://x/hd.flv"},
                "hls_pull_url_map": {"FULL_HD1": "http://x/hd.m3u8"},
                "live_core_sdk_data": {"pull_data": {"stream_data": stream_data}},
            },
        }],
    }}


def test_from_enter_payload():
    info = room.from_enter_payload(enter_payload(), "123456789")

    assert info.ok is True and info.living is True
    assert info.nickname == "夜航西飞"
    assert info.title == "深夜飞行"
    assert info.room_id == "7300000000000000001"
    assert info.online == "1.2万"
    assert info.avatar == "http://x/avatar.jpg"
    assert info.cover == "http://x/cover.jpg"


def test_原画地址来自内嵌的_live_core_sdk_data():
    """原画只出现在那个内嵌 JSON 字符串里，接口顶层是没有的。"""
    info = room.from_enter_payload(enter_payload(), "123456789")
    assert info.flv["origin"] == "http://x/origin.flv"
    assert info.flv["FULL_HD1"] == "http://x/hd.flv"
    assert info.hls["origin"] == "http://x/origin.m3u8"


def test_未开播时也能解析出昵称():
    info = room.from_enter_payload(enter_payload(status=4), "123456789")
    assert info.ok is True and info.living is False
    assert info.nickname == "夜航西飞"


@pytest.mark.parametrize("payload", [
    {}, {"data": None}, {"data": {}}, {"data": {"data": []}}, {"data": "字符串"},
])
def test_残缺的_payload_返回_None(payload):
    assert room.from_enter_payload(payload, "1") is None


def test_内嵌_JSON_坏掉时其余字段照常解析():
    payload = enter_payload()
    payload["data"]["data"][0]["stream_url"]["live_core_sdk_data"] = {"pull_data": {
        "stream_data": "{不是合法 json"}}
    info = room.from_enter_payload(payload, "1")

    assert info.ok is True
    assert info.flv == {"FULL_HD1": "http://x/hd.flv"}       # 顶层那份还在
    assert "origin" not in info.flv


def test_空地址被过滤掉():
    payload = enter_payload()
    payload["data"]["data"][0]["stream_url"]["flv_pull_url"] = {
        "FULL_HD1": "http://x/hd.flv", "HD1": ""}
    info = room.from_enter_payload(payload, "1")
    assert "HD1" not in info.flv
