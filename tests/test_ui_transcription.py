"""模型下载不阻塞界面；逐场转写必须显式确认且具有本地依赖。"""

import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMessageBox

from dylive.ui import transcription as ui
from dylive.ui.settings import AppSettings, SettingsDialog


def wait(qapp, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def models(monkeypatch, tmp_path):
    downloaded = set()
    monkeypatch.setattr(ui.transcription, "MODELS", tuple(SimpleNamespace(
        key=key, label=key.title(), size_mb=size, description="测试模型 " + key)
        for key, size in [("base", 150), ("small", 500)]))
    monkeypatch.setattr(ui.transcription, "is_model_downloaded", downloaded.__contains__)
    monkeypatch.setattr(ui.transcription, "model_directory", lambda key: tmp_path / key)
    monkeypatch.setattr(ui.transcription, "dependency_available", lambda: True)
    return downloaded


def test_model_download_runs_in_background_and_enables_manual_start(qapp, monkeypatch, models):
    entered, release = threading.Event(), threading.Event()
    threads = []

    def download(key, *, progress, cancelled):
        threads.append(threading.get_ident())
        progress(.25, "测试下载进度")
        entered.set()
        assert release.wait(3)
        assert not cancelled()
        models.add(key)

    monkeypatch.setattr(ui.transcription, "download_model", download)
    dialog = ui.TranscriptionDialog(AppSettings())
    heartbeats = []
    QTimer.singleShot(0, lambda: heartbeats.append(True))
    try:
        assert not dialog.start_button.isEnabled()
        dialog.panel.download_button.click()
        assert entered.wait(1)
        wait(qapp, lambda: bool(heartbeats) and dialog.panel.progress.value() == 25)
        assert threads != [threading.get_ident()]
        assert not dialog.panel.model_combo.isEnabled()
        assert not dialog.start_button.isEnabled()
        release.set()
        wait(qapp, lambda: dialog.start_button.isEnabled())
        assert dialog.panel.progress.value() == 100
        dialog.start_button.click()
        assert dialog.result() == QDialog.Accepted
        assert dialog.selected_options() == {
            "model": "small", "language": "auto", "subtitle_mode": "combined"}
    finally:
        release.set()
        dialog.reject()


def test_download_error_restores_controls_and_does_not_allow_transcription(qapp,
                                                                         monkeypatch, models):
    def download(*_args, **_kwargs):
        raise OSError("连接超时")

    monkeypatch.setattr(ui.transcription, "download_model", download)
    dialog = ui.TranscriptionDialog(AppSettings())
    try:
        dialog.panel.download_button.click()
        wait(qapp, lambda: not dialog.panel.busy)
        assert "连接超时" in dialog.panel.status.text()
        assert dialog.panel.download_button.isEnabled()
        assert dialog.panel.model_combo.isEnabled()
        assert not dialog.start_button.isEnabled()
        dialog.accept()
        assert dialog.result() != QDialog.Accepted
    finally:
        dialog.reject()


def test_closing_dialog_cancels_active_download(qapp, monkeypatch, models):
    entered, cancelled = threading.Event(), threading.Event()

    def download(_key, *, progress, cancelled: object):
        entered.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.01)
        assert cancelled()
        raise ui.transcription.TranscriptionCancelled("取消")

    monkeypatch.setattr(ui.transcription, "download_model", download)
    dialog = ui.TranscriptionDialog(AppSettings())
    dialog.panel.busyChanged.connect(lambda busy: cancelled.set() if not busy else None)
    dialog.panel.download_button.click()
    assert entered.wait(1)
    dialog.reject()
    wait(qapp, cancelled.is_set)
    assert "已取消" in dialog.panel.status.text()
    assert models == set()


def test_missing_dependency_does_not_prevent_model_management(qapp, monkeypatch, models):
    models.add("small")
    monkeypatch.setattr(ui.transcription, "dependency_available", lambda: False)
    dialog = ui.TranscriptionDialog(AppSettings())
    try:
        assert dialog.panel.remove_button.isEnabled()
        assert not dialog.start_button.isEnabled()
        assert ".[speech]" in dialog.panel.dependency_notice.text()
        dialog.panel.model_combo.setCurrentIndex(0)
        assert dialog.panel.download_button.isEnabled()
    finally:
        dialog.reject()


def test_model_removal_updates_readiness_and_preserves_existing_media(qapp, monkeypatch, models):
    models.add("small")
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_args: QMessageBox.Yes)
    monkeypatch.setattr(ui.transcription, "remove_model", models.remove)
    dialog = ui.TranscriptionDialog(AppSettings())
    try:
        assert dialog.start_button.isEnabled()
        dialog.panel.remove_button.click()
        assert models == set()
        assert not dialog.start_button.isEnabled()
        assert dialog.panel.download_button.isEnabled()
    finally:
        dialog.reject()


def test_incomplete_model_can_be_removed_and_downloaded_again(qapp, monkeypatch, tmp_path, models):
    directory = tmp_path / "small"
    directory.mkdir()  # 模拟下载中断后只剩目录，没有有效清单。
    monkeypatch.setattr(ui.transcription, "model_directory", lambda key: tmp_path / key)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_args: QMessageBox.Yes)
    monkeypatch.setattr(ui.transcription, "remove_model", lambda key: (tmp_path / key).rmdir())

    def download(key, *, progress, cancelled):
        assert not cancelled()
        (tmp_path / key).mkdir()
        models.add(key)

    monkeypatch.setattr(ui.transcription, "download_model", download)
    dialog = ui.TranscriptionDialog(AppSettings())
    try:
        assert "不完整" in dialog.panel.model_status.text()
        assert dialog.panel.remove_button.isEnabled()
        assert not dialog.panel.download_button.isEnabled()
        assert not dialog.start_button.isEnabled()
        dialog.panel.remove_button.click()
        assert not directory.exists()
        assert dialog.panel.download_button.isEnabled()
        dialog.panel.download_button.click()
        wait(qapp, lambda: dialog.start_button.isEnabled())
        assert directory.is_dir() and "small" in models
    finally:
        dialog.reject()


def test_speech_settings_roundtrip_and_room_profile_hides_global_model_controls(qapp,
                                                                             monkeypatch,
                                                                             tmp_path, models):
    monkeypatch.setattr("dylive.ui.settings.config_path", lambda: tmp_path / "config.json")
    settings = AppSettings(speech_model="base", speech_language="en", speech_subtitle_mode="voice")
    dialog = SettingsDialog(settings)
    try:
        assert dialog.speech_panel.selected_options() == {
            "model": "base", "language": "en", "subtitle_mode": "voice"}
        panel = dialog.speech_panel
        panel.model_combo.setCurrentIndex(panel.model_combo.findData("small"))
        panel.language_combo.setCurrentIndex(panel.language_combo.findData("zh"))
        panel.subtitle_mode_combo.setCurrentIndex(panel.subtitle_mode_combo.findData("combined"))
        dialog.apply_to(settings)
        settings.save()
        back = AppSettings.load()
        assert (back.speech_model, back.speech_language, back.speech_subtitle_mode) == (
            "small", "zh", "combined")
        from dylive.ui.settings import RoomProfileDialog
        profile = RoomProfileDialog(settings, "123", "主播")
        assert profile.speech_panel.parentWidget().isHidden()
        profile.reject()
    finally:
        dialog.reject()
