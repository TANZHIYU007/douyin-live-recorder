"""配置落盘与设置对话框。

配置里存着监测列表 —— 一个守候型工具最不能丢的就是它。
"""

import json

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
