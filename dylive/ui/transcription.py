"""按需下载语音模型，以及手动选择一场录像的转写选项。"""

from __future__ import annotations

import math
import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QLabel, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import transcription
from .widgets import Field


LANGUAGES = [("自动识别", "auto"), ("中文", "zh"), ("英语", "en"),
             ("日语", "ja"), ("韩语", "ko")]
SUBTITLE_MODES = [("独立语音/弹幕轨 + 同时显示兼容轨（推荐）", "combined"),
                  ("仅独立语音/弹幕轨（播放器需支持双字幕）", "voice")]


def _select(combo: QComboBox, value, fallback: str) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else max(0, combo.findData(fallback)))


class _DownloadSignals(QObject):
    progress = Signal(float, str)
    finished = Signal(bool, str)


class SpeechSettingsPanel(QWidget):
    """模型下载独立于设置保存；转写仍须从录像库逐场手动发起。"""

    readyChanged = Signal(bool)
    changed = Signal()
    busyChanged = Signal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._busy = False
        self._ready = False
        self._cancel = threading.Event()
        # 回调仅持有事件和信号对象，窗口销毁后不再访问 QWidget。
        cancel = self._cancel
        self.destroyed.connect(lambda *_: cancel.set())
        self._signals = _DownloadSignals(self)
        self._signals.progress.connect(self._download_progress)
        self._signals.finished.connect(self._download_finished)
        self._download_thread: threading.Thread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.model_combo = QComboBox()
        for model in transcription.MODELS:
            self.model_combo.addItem(f"{model.label} · 约 {model.size_mb:,} MB", model.key)
        _select(self.model_combo, getattr(settings, "speech_model", "small"), "small")
        layout.addWidget(Field("识别模型", self.model_combo, stretch=1))
        self.model_description = QLabel()
        self.model_description.setWordWrap(True)
        self.model_description.setObjectName("hint")
        layout.addWidget(self.model_description)
        self.model_status = QLabel()
        layout.addWidget(self.model_status)

        actions = QHBoxLayout()
        self.download_button = QPushButton("下载模型")
        self.download_button.setObjectName("ghost")
        self.cancel_download_button = QPushButton("取消下载")
        self.cancel_download_button.setEnabled(False)
        self.remove_button = QPushButton("删除模型")
        self.import_button = QPushButton("导入本地模型…")
        self.remove_button.setObjectName("ghost")
        for button in (self.download_button, self.import_button, self.cancel_download_button, self.remove_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        note = QLabel("模型保存在本机，下载一次后可离线使用。\n"
                      "语音转文字仅在录像库中手动发起，不会自动处理每场录制。")
        note.setWordWrap(True)
        note.setObjectName("hint")
        layout.addWidget(note)

        self.language_combo = QComboBox()
        for label, key in LANGUAGES:
            self.language_combo.addItem(label, key)
        _select(self.language_combo, getattr(settings, "speech_language", "auto"), "auto")
        layout.addWidget(Field("语音语言", self.language_combo, stretch=1))
        self.subtitle_mode_combo = QComboBox()
        self.subtitle_mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.subtitle_mode_combo.setMinimumContentsLength(18)
        for label, key in SUBTITLE_MODES:
            self.subtitle_mode_combo.addItem(label, key)
        _select(self.subtitle_mode_combo,
                getattr(settings, "speech_subtitle_mode", "combined"), "combined")
        layout.addWidget(Field("字幕显示", self.subtitle_mode_combo, stretch=1))
        subtitle_note = QLabel("语音字幕和弹幕保留为独立字幕轨，可在播放器中分别选择或关闭。\n"
                               "播放器每次只能显示一条字幕轨时，可选择“语音＋弹幕”兼容轨同时显示；"
                               "语音位于底部中央，弹幕避开底部字幕区域。")
        subtitle_note.setWordWrap(True)
        subtitle_note.setObjectName("hint")
        layout.addWidget(subtitle_note)
        self.dependency_notice = QLabel()
        self.dependency_notice.setWordWrap(True)
        self.dependency_notice.setTextFormat(Qt.PlainText)
        self.dependency_notice.setObjectName("hint")
        layout.addWidget(self.dependency_notice)
        source_note = QLabel("SenseVoice / Paraformer 从魔搭下载；Whisper 从 Hugging Face 下载，"
                             "大陆网络可能无法直连。也可导入从其他电脑取得的本地模型文件夹。")
        source_note.setWordWrap(True)
        source_note.setObjectName("hint")
        layout.addWidget(source_note)
        self.model_combo.currentIndexChanged.connect(self._selection_changed)
        self.language_combo.currentIndexChanged.connect(self.changed)
        self.subtitle_mode_combo.currentIndexChanged.connect(self.changed)
        self.download_button.clicked.connect(self.download_selected)
        self.import_button.clicked.connect(self.import_selected)
        self.cancel_download_button.clicked.connect(self.cancel_download)
        self.remove_button.clicked.connect(self.remove_selected)
        self.refresh()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def busy(self) -> bool:
        return self._busy

    def selected_options(self) -> dict:
        return {"model": self.model_combo.currentData(),
                "language": self.language_combo.currentData(),
                "subtitle_mode": self.subtitle_mode_combo.currentData()}

    def apply_to(self, settings) -> None:
        values = self.selected_options()
        settings.speech_model = values["model"]
        settings.speech_language = values["language"]
        settings.speech_subtitle_mode = values["subtitle_mode"]

    def refresh(self) -> None:
        key = self.model_combo.currentData()
        model = next((m for m in transcription.MODELS if m.key == key), None)
        installed = bool(model and transcription.is_model_downloaded(key))
        present = installed or bool(model and transcription.model_directory(key).exists())
        available = transcription.model_dependency_available(key) if model else False
        self.model_description.setText(model.description if model else "请选择识别模型")
        chinese_only = model is not None and getattr(model, "engine", "whisper") == "paraformer"
        for index in range(self.language_combo.count()):
            self.language_combo.model().item(index).setEnabled(
                not chinese_only or self.language_combo.itemData(index) in {"auto", "zh"})
        if chinese_only and self.language_combo.currentData() not in {"auto", "zh"}:
            self.language_combo.setCurrentIndex(self.language_combo.findData("auto"))
        self.model_status.setText("已下载，可离线使用" if installed else
                                 "本地模型文件不完整或损坏，请先删除后重新下载" if present else
                                 "尚未下载此模型")
        self.model_combo.setEnabled(not self._busy)
        self.download_button.setEnabled(bool(model) and not present and not self._busy)
        self.import_button.setEnabled(bool(model) and not present and not self._busy)
        self.remove_button.setEnabled(present and not self._busy)
        self.cancel_download_button.setEnabled(self._busy and not self._cancel.is_set())
        self.dependency_notice.setVisible(not available)
        if not available:
            self.dependency_notice.setText(
                "此安装包未包含语音识别组件，请使用包含语音转文字支持的版本。"
                if getattr(sys, "frozen", False) else
                '语音识别组件尚未安装。可在项目目录运行：python -m pip install ".[speech]"')
        self._ready = installed and available and not self._busy
        self.readyChanged.emit(self._ready)

    def _selection_changed(self, _index: int) -> None:
        self.status.clear()
        self.progress.setVisible(False)
        self.refresh()
        self.changed.emit()

    def import_selected(self) -> None:
        if self._busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择所选模型的完整文件夹")
        if folder:
            from pathlib import Path
            self.download_selected(folder=Path(folder))

    def download_selected(self, _checked=False, *, folder=None) -> None:
        if self._busy:
            return
        key = self.model_combo.currentData()
        if not key or transcription.is_model_downloaded(key):
            self.refresh()
            return
        self._cancel.clear()
        self._busy = True
        self.progress.setValue(0)
        self.progress.setVisible(True)
        operation = "导入" if folder is not None else "下载"
        self.status.setText("准备%s…" % operation)
        self.cancel_download_button.setText("取消" + operation)
        self.refresh()
        self.busyChanged.emit(True)
        signals, cancel = self._signals, self._cancel

        def report(value: float, message: str) -> None:
            try:
                signals.progress.emit(float(value), message)
            except RuntimeError:
                cancel.set()  # 窗口的 QObject 已销毁。

        def work() -> None:
            try:
                if folder is None:
                    transcription.download_model(key, progress=report, cancelled=cancel.is_set)
                else:
                    transcription.import_model(key, folder, progress=report, cancelled=cancel.is_set)
                ok, message = True, "模型已就绪，可离线使用。"
            except transcription.TranscriptionCancelled:
                ok, message = False, "已取消%s，可稍后重试。" % operation
            except Exception as exc:  # noqa: BLE001 - 下载失败只反馈给当前面板。
                ok, message = False, operation + "失败：" + str(exc)
            try:
                signals.finished.emit(ok, message)
            except RuntimeError:
                pass

        self._download_thread = threading.Thread(target=work, name="speech-model-download",
                                                  daemon=True)
        self._download_thread.start()

    def cancel_download(self) -> None:
        if self._busy:
            self._cancel.set()
            self.cancel_download_button.setEnabled(False)
            self.status.setText("正在取消下载…")

    def _download_progress(self, fraction: float, message: str) -> None:
        if not self._busy or self._cancel.is_set():
            return
        if math.isfinite(fraction):
            self.progress.setValue(round(max(0.0, min(1.0, fraction)) * 100))
        self.status.setText(message)

    def _download_finished(self, ok: bool, message: str) -> None:
        self._busy = False
        self.status.setText(message)
        if ok:
            self.progress.setValue(100)
        self.refresh()
        self.busyChanged.emit(False)

    def remove_selected(self) -> None:
        if self._busy:
            return
        key = self.model_combo.currentData()
        if not key or not (transcription.is_model_downloaded(key)
                           or transcription.model_directory(key).exists()):
            self.refresh()
            return
        answer = QMessageBox.question(self, "删除语音模型",
            "删除所选模型以释放磁盘空间？之后转写需要重新下载。\n已有视频和字幕不会删除。")
        if answer != QMessageBox.Yes:
            return
        try:
            transcription.remove_model(key)
        except Exception as exc:  # noqa: BLE001 - 例如模型正在另一处使用。
            self.status.setText("无法删除模型：" + str(exc))
        else:
            self.status.setText("模型已删除。")
        self.refresh()


class TranscriptionDialog(QDialog):
    """返回选项；持久化任务由录像库统一加入现有处理队列。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音转文字")
        self.resize(610, 680)
        outer = QVBoxLayout(self)
        note = QLabel("仅转写当前选中的这场录像。原视频会保留，生成的语音字幕可与弹幕同时显示。")
        note.setWordWrap(True)
        outer.addWidget(note)
        self.panel = SpeechSettingsPanel(settings)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.panel)
        outer.addWidget(scroll, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.start_button = self.buttons.button(QDialogButtonBox.Ok)
        self.start_button.setText("加入转写队列")
        self.start_button.setObjectName("primary")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.start_button.setEnabled(self.panel.ready)
        self.panel.readyChanged.connect(self.start_button.setEnabled)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.finished.connect(self.panel.cancel_download)
        outer.addWidget(self.buttons)

    def selected_options(self) -> dict:
        return self.panel.selected_options()

    def accept(self) -> None:
        # 下载状态可能在按钮点击前发生变化，最终入队前再确认一次。
        self.panel.refresh()
        if self.panel.ready:
            super().accept()
