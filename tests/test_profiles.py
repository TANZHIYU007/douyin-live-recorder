import json
from dataclasses import asdict
from types import SimpleNamespace

from dylive.profiles import clean_profiles, clean_recording


def test_profile_validation_drops_unknown_unsafe_or_bad_values():
    assert clean_recording({"quality": "typo", "container": "exe", "subtitle_size": 999,
                            "subtitle_delay": float("nan"), "segment_minutes": True,
                            "headful": True, "cookie": "secret", "record_video": "false",
                            "kinds": ["chat", "unknown", None, "chat"],
                            "out_dir": "  custom  "}) == {"kinds": ["chat"], "out_dir": "custom"}
    assert clean_profiles({"bad-link": {"quality": "HD1"}, "１２３": {"quality": "HD1"},
                           "1": None, "2": {"quality": "HD1"}}) == {"2": {"quality": "HD1"}}


def test_per_room_settings_inherit_and_are_isolated(qapp, tmp_path):
    from dylive.ui.settings import AppSettings
    from dylive.ui.window import MainWindow
    settings = AppSettings(out_dir=str(tmp_path), quality="origin", kinds=["chat"],
                           room_profiles={"1": {"quality": "HD1", "segment_minutes": 7,
                                                 "out_dir": str(tmp_path / "one")},
                                          "2": {"record_video": False}})
    first = MainWindow._make_options(SimpleNamespace(settings=settings), "1")
    second = MainWindow._make_options(SimpleNamespace(settings=settings), "2")
    assert first.quality == "HD1" and first.segment_seconds == 420
    assert first.out_dir == tmp_path / "one"
    assert not second.record_video and second.quality == "origin"
    settings.quality = "SD1"
    assert settings.effective_for("1").quality == "HD1"
    assert settings.effective_for("2").quality == "SD1"
    settings.effective_for("3").kinds.append("gift")
    assert settings.kinds == ["chat"]
    assert settings.recording_roots() == [tmp_path, tmp_path / "one"]


def test_profiles_persist_and_validate_nested_config(qapp, tmp_path, monkeypatch):
    from dylive.ui import settings as module
    config = tmp_path / "config.json"
    monkeypatch.setattr(module, "config_path", lambda: config)
    settings = module.AppSettings(room_profiles={"1": {"quality": "HD1", "subtitle_delay": 2.5}})
    settings.save()
    assert module.AppSettings.load().room_profiles == settings.room_profiles
    config.write_text(json.dumps({"quality": "SD1", "room_profiles": ["broken"]}))
    assert module.AppSettings.load().room_profiles == {}
    assert module.AppSettings.load().quality == "SD1"


def test_profile_dialog_save_reset_and_cancel_never_change_global(qapp):
    from dylive.ui.settings import AppSettings, RoomProfileDialog
    settings = AppSettings(quality="SD1", headful=True, theme="dark")
    original = asdict(settings)
    dialog = RoomProfileDialog(settings, "123", "主播")
    assert not dialog.quality.isEnabled()
    dialog.independent.setChecked(True)
    dialog.quality.setCurrentIndex(dialog.quality.findData("HD1"))
    dialog.segment.setValue(9)
    dialog.cb_danmaku.setChecked(False)
    dialog.apply_to(settings)
    assert settings.effective_for("123").quality == "HD1"
    assert settings.effective_for("123").segment_minutes == 9
    assert not settings.effective_for("123").record_danmaku
    assert "headful" not in settings.room_profiles["123"]
    assert settings.quality == "SD1" and settings.headful and settings.theme == "dark"
    dialog.reject()
    cancelled = RoomProfileDialog(settings, "123", "主播")
    cancelled.quality.setCurrentIndex(0)
    cancelled.reject()
    assert settings.effective_for("123").quality == "HD1"
    reset = RoomProfileDialog(settings, "123", "主播")
    reset.independent.setChecked(False)
    reset.apply_to(settings)
    assert asdict(settings) == original
    reset.reject()
