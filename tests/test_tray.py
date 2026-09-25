"""托盘图标与主窗口的冒烟测试。

没有真托盘的环境（CI、offscreen）里 Tray 会整个退化成空操作 —— 这正是要
钉住的一条：调用方不该为「这台机器没有托盘」到处写 if。
"""

import pytest

pytest.importorskip("PySide6", reason="没装 PySide6，跳过界面相关用例")

from dylive.ui import tray as tray_mod                     # noqa: E402
from dylive.ui.settings import AppSettings                 # noqa: E402


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("dylive.ui.settings.config_path", lambda: path)
    return path


def make(parent=None, **kw):
    noop = kw.pop("noop", lambda: None)
    return tray_mod.Tray(parent, on_show=noop, on_start_all=noop,
                         on_stop_all=noop, on_quit=noop, **kw)


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------

def test_图标画得出来且有多个尺寸(qapp):
    icon = tray_mod.make_icon(active=True)
    assert not icon.isNull()
    assert len(icon.availableSizes()) >= 4


def test_在录和守候画的不是同一张图(qapp):
    """扫一眼托盘就该知道现在在不在录。"""
    busy = tray_mod.make_icon(True).pixmap(32, 32).toImage()
    idle = tray_mod.make_icon(False).pixmap(32, 32).toImage()
    assert busy != idle


# --------------------------------------------------------------------------
# 退化成空操作
# --------------------------------------------------------------------------

def test_用户关掉托盘时所有方法都是空操作(qapp):
    t = make(enabled=False)
    assert t.available is False

    # 全都不该抛
    t.set_active(True, "录制中 1 / 2")
    t.notify("开播了", "夜航西飞")
    t.explain_hidden_once()
    t.hide()


def test_没有托盘的环境里也不会崩(qapp):
    """CI 和 offscreen 下 isSystemTrayAvailable() 是 False。"""
    t = make(enabled=True)
    t.set_active(True, "x")
    t.notify("标题", "内容")
    t.explain_hidden_once()
    t.hide()


def test_收进托盘的说明只弹一次(qapp, monkeypatch):
    said = []
    t = make(enabled=False)
    monkeypatch.setattr(t, "notify", lambda title, msg: said.append(title))
    t._icon = object()          # 骗过「没有托盘就直接返回」那道判断

    t.explain_hidden_once()
    t.explain_hidden_once()
    t.explain_hidden_once()
    assert len(said) == 1


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------

def test_托盘相关设置能存能读(conf):
    s = AppSettings()
    s.tray = False
    s.minimize_to_tray = True
    s.notify_live = False
    s.save()

    back = AppSettings.load()
    assert back.tray is False
    assert back.minimize_to_tray is True
    assert back.notify_live is False


def test_默认开托盘但不改变关窗口的行为():
    """默认值要保守：关窗口就是退出，这是大多数人预期的。"""
    s = AppSettings()
    assert s.tray is True
    assert s.minimize_to_tray is False
    assert s.notify_live is True


def test_关掉托盘时收进托盘和通知一起变灰(qapp, conf):
    from dylive.ui.settings import SettingsDialog

    s = AppSettings()
    s.tray = False
    dialog = SettingsDialog(s)
    assert dialog.cb_minimize.isEnabled() is False
    assert dialog.cb_notify.isEnabled() is False

    dialog.cb_tray.setChecked(True)
    assert dialog.cb_minimize.isEnabled() is True


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

def test_主窗口能建起来并跑一轮刷新(qapp, conf, monkeypatch):
    """构造 + 一轮 tick 能跑通，绝大多数接线错误都会在这里暴露。"""
    from dylive.ui.window import MainWindow

    window = MainWindow()
    try:
        assert window.tray is not None
        window._on_tick()               # 不该抛
        window._refresh_rows()
        assert window.rows == {}
    finally:
        window.manager.shutdown(timeout=5)
        window.close()


class 假房间:
    def __init__(self, name="夜航西飞", title="深夜飞行"):
        self.web_rid = "123456789"
        self.info = type("I", (), {"title": title})()
        self._name = name

    def display_name(self):
        return self._name


class 假状态:
    def __init__(self, elapsed=3661.0, total_bytes=2 << 30):
        self.elapsed = elapsed
        self.total_bytes = total_bytes


@pytest.fixture
def window(qapp, conf):
    from dylive.ui.window import MainWindow

    win = MainWindow()
    yield win
    win.manager.shutdown(timeout=5)
    win.close()


def test_开播和收工各通知一次(window, monkeypatch):
    from dylive.manager import RECORDING, WAITING

    sent = []
    monkeypatch.setattr(window.tray, "notify", lambda t, m: sent.append((t, m)))
    entry, st = 假房间(), 假状态()

    window._note_state(entry, WAITING, st)      # 第一次见到，只记状态不通知
    assert sent == []

    window._note_state(entry, RECORDING, st)
    assert len(sent) == 1 and "开播了" in sent[0][0] and "夜航西飞" in sent[0][0]

    window._note_state(entry, RECORDING, st)    # 状态没变，不该再响
    assert len(sent) == 1

    window._note_state(entry, WAITING, st)
    assert len(sent) == 2 and "收工" in sent[1][0]
    assert "01:01:01" in sent[1][1]             # 时长从录制中那会儿记下来的
    assert "GB" in sent[1][1]


def test_关掉通知就不响(window, monkeypatch):
    from dylive.manager import RECORDING, WAITING

    sent = []
    monkeypatch.setattr(window.tray, "notify", lambda t, m: sent.append(t))
    window.settings.notify_live = False

    entry, st = 假房间(), 假状态()
    window._note_state(entry, WAITING, st)
    window._note_state(entry, RECORDING, st)
    window._note_state(entry, WAITING, st)
    assert sent == []


def test_进入处理中时说的是正在封装(window, monkeypatch):
    from dylive.manager import PROCESSING, RECORDING

    sent = []
    monkeypatch.setattr(window.tray, "notify", lambda t, m: sent.append((t, m)))
    entry, st = 假房间(), 假状态()

    window._note_state(entry, RECORDING, st)
    window._note_state(entry, PROCESSING, st)
    assert "录完了" in sent[-1][0]
    assert "封装" in sent[-1][1]
