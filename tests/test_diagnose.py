"""链路诊断的报告逻辑。

真正的诊断要连真实直播间，测不了；但「怎么把结果讲清楚」这部分是纯逻辑，
而且它恰恰是这个工具的全部价值 —— 报告说不清，等于没诊断。
"""

from collections import Counter


from dylive import room
from dylive.diagnose import Report, Step, _step_network, _step_room


def test_有一步失败整体就算失败():
    r = Report()
    r.add(Step("网络", True, "HTTP 200"))
    r.add(Step("弹幕连接", False, "没挂上", hint="开 --headful 看看"))
    assert r.ok is False


def test_全通过():
    r = Report()
    r.add(Step("网络", True, "HTTP 200"))
    r.add(Step("解码", True, "解出 120 个事件"))
    assert r.ok is True
    assert "整条链路都通" in r.render("123")


def test_失败时把建议一起打出来():
    step = Step("风控", False, "被挡在验证页", hint="加 --headful 重跑")
    assert "→ 加 --headful 重跑" in step.render()


def test_通过时不打建议():
    """通过了还甩一句「你该怎么办」只会让人以为出了问题。"""
    step = Step("风控", True, "没有落在验证页", hint="加 --headful 重跑")
    assert "→" not in step.render()


def test_报告里列出各类事件的条数():
    r = Report()
    r.add(Step("解码", True, "ok"))
    r.counts = Counter({"chat": 120, "member": 4000, "gift": 3})
    text = r.render("123")

    assert "member       4000" in text
    assert "chat         120" in text
    # 多的排前面，一眼看得出这个房间是什么情况
    assert text.index("member") < text.index("chat")


def test_报告头部带上环境信息():
    """用户把报告贴过来时，这几行省掉一轮「你什么系统、怎么装的」。"""
    text = Report().render("123456789")
    assert "目标直播间: 123456789" in text
    assert "Python" in text
    assert "运行方式" in text


# --------------------------------------------------------------------------
# 各段的判定
# --------------------------------------------------------------------------

class 假响应:
    def __init__(self, status=200, cookies=()):
        self.status_code = status
        self.cookies = [type("C", (), {"name": n})() for n in cookies]


class 假会话:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.cookies = {}

    def get(self, *a, **kw):
        if self._exc:
            raise self._exc
        return self._resp


def test_网络这段认_ttwid():
    step = _step_network(假会话(假响应(200, ["ttwid"])))
    assert step.ok is True and "ttwid" in step.detail


def test_拿不到_ttwid_算通过但会提一句():
    """没有 ttwid 不代表录不了，只是风控会更严 —— 不该直接判失败。"""
    step = _step_network(假会话(假响应(200, [])))
    assert step.ok is True
    assert "风控" in step.detail


def test_网络不通时给的是网络建议():
    import requests
    step = _step_network(假会话(exc=requests.ConnectionError("拒绝连接")))
    assert step.ok is False
    assert "代理" in step.hint or "网络" in step.hint


def test_房间号解析不出来时直接停在这一步(monkeypatch):
    step, info = _step_room("这不是房间号", 假会话())
    assert step.ok is False and info is None
    assert "直播间号" in step.hint


def test_未开播不算程序有问题(monkeypatch):
    """这条最容易被误读成「工具坏了」，所以话必须说清楚。"""
    monkeypatch.setattr(room, "parse_target", lambda t, s=None: "123")
    monkeypatch.setattr(room, "fetch", lambda rid, s=None: room.RoomInfo(
        web_rid=rid, ok=True, status=4, nickname="夜航西飞"))

    step, info = _step_room("123", 假会话())
    assert step.ok is False
    assert "不能说明程序有问题" in step.hint


def test_查不到和未开播给的建议不一样(monkeypatch):
    monkeypatch.setattr(room, "parse_target", lambda t, s=None: "123")
    monkeypatch.setattr(room, "fetch",
                        lambda rid, s=None: room.RoomInfo(web_rid=rid, ok=False))

    step, _ = _step_room("123", 假会话())
    assert step.ok is False
    assert "不代表主播没开播" in step.hint


def test_正在播时列出可用画质(monkeypatch):
    monkeypatch.setattr(room, "parse_target", lambda t, s=None: "123")
    monkeypatch.setattr(room, "fetch", lambda rid, s=None: room.RoomInfo(
        web_rid=rid, ok=True, status=2, nickname="夜航西飞", title="深夜飞行",
        flv={"origin": "u", "HD1": "u"}, hls={"SD2": "u"}))

    step, info = _step_room("123", 假会话())
    assert step.ok is True
    assert info.living is True
    for quality in ("origin", "HD1", "SD2"):
        assert quality in step.detail
