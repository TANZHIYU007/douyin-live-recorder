"""全局设置：数据结构、落盘、设置对话框。

监测列表和设置都存在 paths.app_data_dir()/config.json —— 一个守候型工具
每次启动都要重新加房间是不能忍的。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import paths, runtime
from ..messages import DEFAULT_KINDS, KIND_BY_METHOD
from .theme import KIND_LABELS
from .widgets import Card, Field, separator

log = logging.getLogger("settings")

ALL_KINDS = [k for k in ("chat", "emoji", "gift", "social", "member", "like",
                         "control", "fansclub", "user_seq", "stats", "room_notice")
             if k in set(KIND_BY_METHOD.values())]

QUALITIES = [("原画", "origin"), ("蓝光", "FULL_HD1"), ("超清", "HD1"),
             ("高清", "SD1"), ("标清", "SD2")]

# 预览的解码宽度和帧率。都只影响窗口里那块画面，跟录下来的文件毫无关系。
PREVIEW_WIDTHS = [("流畅 480", 480), ("标准 720", 720),
                  ("清晰 960", 960), ("最高 1280", 1280)]
PREVIEW_FPS = [("跟随直播源", 0), ("最高 30", 30), ("最高 20", 20),
               ("最高 10", 10), ("省电 5", 5)]


def _nearest(options, value) -> int:
    """按数值挑最接近的一项，配置里存了个不在列表里的值也不至于错位。"""
    return min(range(len(options)), key=lambda i: abs(options[i][1] - value))


def config_path() -> Path:
    return paths.app_data_dir() / "config.json"


@dataclass
class AppSettings:
    out_dir: str = ""
    quality: str = "origin"
    container: str = "mp4"
    segment_minutes: int = 0
    record_video: bool = True
    record_danmaku: bool = True
    write_xml: bool = True
    kinds: List[str] = field(default_factory=lambda: list(DEFAULT_KINDS))
    headful: bool = False
    keep_login: bool = False
    preview: bool = True
    preview_width: int = 720            # 预览解码宽度，源比这窄就不放大
    preview_fps: int = 0                # 0 = 跟随直播源，不限帧
    embed_subtitle: bool = True         # 录完自动把弹幕封成软字幕
    subtitle_replace: bool = True       # 封装成功后用 mkv 替换原视频
    subtitle_size: int = 48
    subtitle_duration: float = 10.0
    subtitle_reserve: float = 0.4
    theme: str = "light"
    rooms: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.out_dir:
            self.out_dir = str(runtime.default_output_dir())

    # -- 落盘 -------------------------------------------------------------

    @classmethod
    def load(cls) -> "AppSettings":
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}      # 忽略旧版本残留的字段
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)


class SettingsDialog(QDialog):
    """所有房间共用的录制设置。"""

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("录制设置")
        self._settings = settings

        # 卡片攒起来有八百多像素高，1366x768 的笔记本上直接放不下，
        # 所以内容区放进滚动条里，按钮固定在底部始终可见
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(14)
        scroll.setWidget(content)

        card = Card("录制")
        self.out_edit = QLineEdit(settings.out_dir)
        self.out_edit.setToolTip(settings.out_dir)
        self.out_edit.textChanged.connect(self.out_edit.setToolTip)
        browse = QPushButton("…")
        browse.setObjectName("ghost")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._pick_folder)
        out_row = _row([(self.out_edit, 1), (browse, 0)])
        card.body.addWidget(Field("输出目录", out_row, stretch=1))

        self.quality = QComboBox()
        self.quality.addItems([label for label, _ in QUALITIES])
        self.quality.setCurrentIndex(
            next((i for i, (_, v) in enumerate(QUALITIES) if v == settings.quality), 0))
        self.quality.setToolTip("拿不到指定画质时会自动往下降级")
        self.container = QComboBox()
        self.container.addItems(["mp4", "flv", "ts", "mkv"])
        self.container.setCurrentText(settings.container)
        card.body.addWidget(Field("画质 / 格式",
                                  _row([(self.quality, 1), (self.container, 1)]),
                                  stretch=1))

        self.segment = QSpinBox()
        self.segment.setRange(0, 1440)
        self.segment.setSuffix(" 分钟")
        self.segment.setSpecialValueText("不分片")
        self.segment.setValue(settings.segment_minutes)
        card.body.addWidget(Field("自动分片", self.segment))

        self.cb_video = QCheckBox("画面")
        self.cb_video.setChecked(settings.record_video)
        self.cb_danmaku = QCheckBox("弹幕")
        self.cb_danmaku.setChecked(settings.record_danmaku)
        self.cb_xml = QCheckBox("同时存 XML")
        self.cb_xml.setChecked(settings.write_xml)
        card.body.addWidget(Field("内容", _row([(self.cb_video, 0), (self.cb_danmaku, 0),
                                               (self.cb_xml, 0)], stretch_end=True),
                                  stretch=1))
        body.addWidget(card)

        kinds_card = Card("记录哪些事件")
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        self.kind_boxes = {}
        for i, kind in enumerate(ALL_KINDS):
            box = QCheckBox(KIND_LABELS.get(kind, kind))
            box.setChecked(kind in settings.kinds)
            self.kind_boxes[kind] = box
            grid.addWidget(box, i // 4, i % 4)
        kinds_card.body.addWidget(grid_host)
        body.addWidget(kinds_card)

        sub_card = Card("弹幕字幕")
        self.cb_embed = QCheckBox("录完自动把弹幕封装成视频里的字幕轨（输出 mkv）")
        self.cb_embed.setChecked(settings.embed_subtitle)
        self.cb_embed.setToolTip("软字幕，不重新编码、画质无损，播放器里可以随时开关")
        self.cb_sub_replace = QCheckBox("封装后删除原始视频（校验通过才删，省一半磁盘）")
        self.cb_sub_replace.setChecked(settings.subtitle_replace)
        sub_card.body.addWidget(self.cb_embed)
        sub_card.body.addWidget(self.cb_sub_replace)

        self.sub_size = QSpinBox()
        self.sub_size.setRange(16, 120)
        self.sub_size.setSuffix(" px")
        self.sub_size.setValue(settings.subtitle_size)
        self.sub_speed = QSpinBox()
        self.sub_speed.setRange(4, 30)
        self.sub_speed.setSuffix(" 秒")
        self.sub_speed.setValue(int(settings.subtitle_duration))
        self.sub_speed.setToolTip("一条弹幕从右侧划到左侧消失所需的时间")
        sub_card.body.addWidget(Field("字号 / 时长",
                                      _row([(self.sub_size, 1), (self.sub_speed, 1)]),
                                      stretch=1))
        self.sub_reserve = QSpinBox()
        self.sub_reserve.setRange(0, 80)
        self.sub_reserve.setSuffix(" %")
        self.sub_reserve.setValue(int(settings.subtitle_reserve * 100))
        self.sub_reserve.setToolTip("屏幕下方留出这么多不放弹幕，免得挡住主播")
        sub_card.body.addWidget(Field("下方留白", self.sub_reserve))
        self.cb_embed.toggled.connect(self._sync_subtitle_enabled)
        body.addWidget(sub_card)

        adv = Card("其他")
        self.cb_preview = QCheckBox("显示实时画面预览（额外拉一路最低画质，不影响录制）")
        self.cb_preview.setChecked(settings.preview)
        adv.body.addWidget(self.cb_preview)

        self.pv_width = QComboBox()
        for label, value in PREVIEW_WIDTHS:
            self.pv_width.addItem(label, value)
        self.pv_width.setCurrentIndex(_nearest(PREVIEW_WIDTHS, settings.preview_width))
        self.pv_width.setToolTip("预览的解码宽度。直播源本身比这窄时按源走，不会放大。")
        self.pv_fps = QComboBox()
        for label, value in PREVIEW_FPS:
            self.pv_fps.addItem(label, value)
        self.pv_fps.setCurrentIndex(_nearest(PREVIEW_FPS, settings.preview_fps))
        self.pv_fps.setToolTip("限帧只省 CPU，不会让画面更清楚；机器吃力时再往下调。")
        adv.body.addWidget(Field("预览清晰度", self.pv_width))
        adv.body.addWidget(Field("预览帧率", self.pv_fps))
        adv.body.addWidget(separator())

        self.cb_headful = QCheckBox("显示浏览器窗口（被风控挡住时可用它手动过验证）")
        self.cb_headful.setChecked(settings.headful)
        self.cb_login = QCheckBox("保持登录态（登录后弹幕更完整，所有房间共用）")
        self.cb_login.setChecked(settings.keep_login)
        for w in (self.cb_headful, self.cb_login):
            adv.body.addWidget(w)
        adv.body.addWidget(separator())
        adv.body.addWidget(QLabel("改动浏览器相关选项后，需要停止再开始才会生效。"),
                           alignment=Qt.AlignLeft)
        adv.body.itemAt(adv.body.count() - 1).widget().setObjectName("hint")
        body.addWidget(adv)

        self._sync_subtitle_enabled(settings.embed_subtitle)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        outer.addWidget(scroll, 1)
        foot = QWidget()
        foot_lay = QHBoxLayout(foot)
        foot_lay.setContentsMargins(20, 10, 20, 14)
        foot_lay.addWidget(buttons)
        outer.addWidget(foot)
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """别超出屏幕：内容再多也只是滚动条变长。"""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry().height() if screen else 900
        self.resize(560, min(860, max(420, avail - 120)))

    def _sync_subtitle_enabled(self, on: bool) -> None:
        for w in (self.cb_sub_replace, self.sub_size, self.sub_speed, self.sub_reserve):
            w.setEnabled(on)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录",
                                                  self.out_edit.text())
        if folder:
            self.out_edit.setText(folder)

    def apply_to(self, settings: AppSettings) -> None:
        settings.out_dir = self.out_edit.text().strip() or settings.out_dir
        settings.quality = QUALITIES[self.quality.currentIndex()][1]
        settings.container = self.container.currentText()
        settings.segment_minutes = self.segment.value()
        settings.record_video = self.cb_video.isChecked()
        settings.record_danmaku = self.cb_danmaku.isChecked()
        settings.write_xml = self.cb_xml.isChecked()
        settings.kinds = [k for k, b in self.kind_boxes.items() if b.isChecked()] \
            or list(DEFAULT_KINDS)
        settings.preview = self.cb_preview.isChecked()
        settings.preview_width = self.pv_width.currentData()
        settings.preview_fps = self.pv_fps.currentData()
        settings.embed_subtitle = self.cb_embed.isChecked()
        settings.subtitle_replace = self.cb_sub_replace.isChecked()
        settings.subtitle_size = self.sub_size.value()
        settings.subtitle_duration = float(self.sub_speed.value())
        settings.subtitle_reserve = self.sub_reserve.value() / 100.0
        settings.headful = self.cb_headful.isChecked()
        settings.keep_login = self.cb_login.isChecked()


def _row(items, stretch_end: bool = False) -> QWidget:
    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for widget, stretch in items:
        lay.addWidget(widget, stretch)
    if stretch_end:
        lay.addStretch(1)
    return host
