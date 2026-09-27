"""配置落盘与设置对话框。

配置里存着监测列表 —— 一个守候型工具最不能丢的就是它。
"""

import json
import os

import pytest

pytest.importorskip("PySide6", reason="没装 PySide6，跳过界面相关用例")

from dylive.recorder import Options                        # noqa: E402
from dylive.ui.settings import AppSettings                 # noqa: E402


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """把配置路径挪到临时目录，别动用户真实的 config.json。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr("dylive.ui.settings.config_path", lambda: path)
    return path


def test_存了再读回来是同一份(conf):
    s = AppSettings()
    s.rooms = ["123456789", "987654321"]
    s.subtitle_delay = 4.5
    s.quality = "FULL_HD1"
    s.save()

    back = AppSettings.load()
    assert back.rooms == ["123456789", "987654321"]
    assert back.subtitle_delay == 4.5
    assert back.quality == "FULL_HD1"


def test_没有配置文件时用默认值(conf):
    s = AppSettings.load()
    assert s.rooms == []
    assert s.subtitle_delay == 0.0
    assert s.out_dir                     # __post_init__ 会填一个默认输出目录


def test_旧版本残留的字段被忽略(conf):
    conf.write_text(json.dumps({"rooms": ["1"], "早就删掉的选项": True}),
                    encoding="utf-8")
    assert AppSettings.load().rooms == ["1"]


def test_配置坏掉时不崩但会退回默认值(conf):
    """这正是配置需要原子写入的理由：写到一半断电，房间列表就全没了。"""
    conf.write_text("{这不是 json", encoding="utf-8")
    s = AppSettings.load()
    assert s.rooms == []


@pytest.mark.parametrize("content", ["null", "[]", '"不是对象"'])
def test_合法_json_但不是配置对象时退回默认值(conf, content):
    conf.write_text(content, encoding="utf-8")
    assert AppSettings.load().rooms == []


def test_错误类型字段被忽略而有效字段保留(conf):
    conf.write_text(json.dumps({
        "rooms": "不应该是字符串",
        "segment_minutes": "会让 Qt 崩溃",
        "subtitle_duration": 8,
        "quality": "HD1",
    }), encoding="utf-8")

    settings = AppSettings.load()
    assert settings.rooms == []
    assert settings.segment_minutes == 0
    assert settings.subtitle_duration == 8.0
    assert settings.quality == "HD1"


def test_保存中断不会破坏原配置(conf, monkeypatch):
    original = AppSettings(rooms=["123"])
    original.save()
    before = conf.read_bytes()

    monkeypatch.setattr(os, "replace",
                        lambda *_: (_ for _ in ()).throw(OSError("模拟断电")))
    AppSettings(rooms=["456"]).save()

    assert conf.read_bytes() == before
    assert not list(conf.parent.glob("config.json.*.tmp"))


def test_延迟补偿默认不开():
    """默认 0 —— 不同网络环境下的拉流延迟不一样，不能替用户拍板。"""
    assert AppSettings().subtitle_delay == 0.0
    assert Options(target="1").subtitle_delay == 0.0


def test_设置对话框能建起来且带上延迟补偿(qapp):
    from dylive.ui.settings import SettingsDialog

    s = AppSettings()
    s.subtitle_delay = 3.5
    dialog = SettingsDialog(s)

    assert dialog.sub_delay.value() == 3.5
    dialog.sub_delay.setValue(6.0)
    dialog.apply_to(s)
    assert s.subtitle_delay == 6.0


def test_主题设置统一放在设置对话框(qapp):
    from dylive.ui.settings import SettingsDialog

    settings = AppSettings(theme="light")
    dialog = SettingsDialog(settings)
    dialog.theme_mode.setCurrentIndex(1)
    dialog.apply_to(settings)

    assert settings.theme == "dark"
    assert dialog.environment_check.text() == "立即检查"


def test_环境检查结果会回写设置页面(qapp, monkeypatch):
    from dylive.ui import settings as settings_mod

    shown = []
    monkeypatch.setattr(settings_mod.QMessageBox, "information",
                        lambda *args: shown.append(args[-1]))
    dialog = settings_mod.SettingsDialog(AppSettings())
    dialog._environment_checked(True, "四项全部通过")

    assert "正常" in dialog.environment_status.text()
    assert dialog.environment_check.text() == "重新检查"
    assert shown == ["四项全部通过"]


def test_关掉自动封装时延迟补偿一起变灰(qapp):
    from dylive.ui.settings import SettingsDialog

    s = AppSettings()
    s.embed_subtitle = False
    dialog = SettingsDialog(s)
    assert dialog.sub_delay.isEnabled() is False

    dialog.cb_embed.setChecked(True)
    assert dialog.sub_delay.isEnabled() is True


def test_事件类型一个都不勾时回落到默认(qapp):
    """一条都不记录等于白录，界面上不该允许出现这种状态。"""
    from dylive.ui.settings import SettingsDialog

    s = AppSettings()
    dialog = SettingsDialog(s)
    for box in dialog.kind_boxes.values():
        box.setChecked(False)
    dialog.apply_to(s)
    assert s.kinds                       # 非空
