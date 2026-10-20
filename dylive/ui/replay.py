"""本地视频播放器及可搜索、随画面滚动的弹幕时间轴。"""

from __future__ import annotations

import bisect
import threading

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QHeaderView, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSlider, QSplitter, QTableView,
                               QVBoxLayout, QWidget)

from ..replay import MAX_EVENTS, Timeline, build_timeline, load_events
from ..utils import clock
from .theme import KIND_LABELS


class EventModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.events = []
        self.visible = []
        self.offsets = []

    def set_events(self, events) -> None:
        self.events = events
        self.filter("")

    def filter(self, query: str) -> None:
        query = query.strip().casefold()
        self.beginResetModel()
        self.visible = [e for e in self.events if not query or e.matches(query)]
        self.offsets = [e.offset for e in self.visible]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.visible)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.visible):
            return None
        event = self.visible[index.row()]
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return (clock(event.offset), KIND_LABELS.get(event.kind, event.kind),
                    event.user, event.content)[index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return ("场内时间", "类型", "用户", "内容")[section]
        return None


class _Loader(QObject):
    loaded = Signal(object, object, str)


class ReplayDialog(QDialog):
    def __init__(self, session, task=None, parent=None, *, seek_offset=None, ffmpeg=""):
        super().__init__(parent)
        self.setWindowTitle("弹幕同步回放 · " + session.streamer + " · " + session.title)
        self.resize(1120, 720)
        self.setMinimumSize(760, 480)
        if self.screen():
            available = self.screen().availableGeometry()
            self.resize(min(1120, available.width() - 40), min(720, available.height() - 60))
        self.timeline = Timeline()
        self.segment_index = -1
        self._pending_seek = None
        self._initial_seek = seek_offset
        self._closed = False
        self._cancel = threading.Event()
        self._last_chat = None
        self._event_offsets = []
        self._loader = _Loader()
        self._loader.loaded.connect(self._loaded)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(.7)
        self.player.setAudioOutput(self.audio)
        self._build()
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(
            lambda value: self.slider.setRange(0, min(2_147_483_647, max(0, value))))
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.seekableChanged.connect(lambda _value: self._apply_pending_seek())
        self.player.playbackStateChanged.connect(lambda state: self.play_btn.setText(
            "暂停" if state == QMediaPlayer.PlayingState else "播放"))
        self.player.errorOccurred.connect(lambda _error, message: self.status.setText(
            "无法播放此分片：" + message + "。可关闭回放后使用外部播放器。"))
        self.finished.connect(self._close_player)
        # Worker 持有独立 reporter，关闭窗口后不再发送结果，也不跨线程碰控件。
        reporter, cancel = self._loader, self._cancel

        def load():
            try:
                events, stats = load_events(session.jsonl, cancel, focus_offset=seek_offset)
                timeline = build_timeline(session, task, ffmpeg=ffmpeg, cancel=cancel)
                notes = [timeline.note] if timeline.note else []
                if stats.skipped:
                    notes.append("跳过 %d 条损坏记录" % stats.skipped)
                if stats.truncated:
                    notes.append("仅载入定位附近最多 %d 条事件；其余记录可在录像库跨场搜索" % MAX_EVENTS
                                 if seek_offset is not None else
                                 "仅载入前 %d 条事件；其余记录可在录像库跨场搜索" % MAX_EVENTS)
                if not cancel.is_set():
                    reporter.loaded.emit(events, timeline, "；".join(notes))
            except Exception as exc:  # noqa: BLE001 - 文件异常需要显示给用户
                if not cancel.is_set():
                    reporter.loaded.emit([], Timeline(), "读取回放失败：" + str(exc))

        threading.Thread(target=load, name="replay-load", daemon=True).start()

    def _build(self):
        outer = QVBoxLayout(self)
        self.status = QLabel("正在读取弹幕和分片时间轴…")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        outer.addWidget(self.status)
        split = QSplitter(Qt.Horizontal)
        left, right = QWidget(), QWidget()
        video_layout, chat_layout = QVBoxLayout(left), QVBoxLayout(right)
        video_layout.setContentsMargins(0, 0, 8, 0)
        chat_layout.setContentsMargins(8, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setMinimumSize(260, 180)
        self.player.setVideoOutput(self.video)
        video_layout.addWidget(self.video, 1)
        self.chat = QLabel("尚无弹幕")
        self.chat.setTextFormat(Qt.PlainText)
        self.chat.setWordWrap(True)
        self.chat.setMinimumHeight(100)
        self.chat.setMaximumHeight(170)
        video_layout.addWidget(self.chat)
        self.segment = QComboBox()
        self.segment.setEnabled(False)
        self.segment.currentIndexChanged.connect(self._choose_segment)
        video_layout.addWidget(self.segment)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderReleased.connect(lambda: self.player.setPosition(self.slider.value()))
        video_layout.addWidget(self.slider)
        controls = QHBoxLayout()
        self.play_btn = QPushButton("播放")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._toggle_play)
        self.position = QLabel("00:00:00 / 00:00:00")
        controls.addWidget(self.play_btn)
        controls.addWidget(self.position, 1)
        self.mute = QCheckBox("静音")
        self.mute.toggled.connect(self.audio.setMuted)
        controls.addWidget(self.mute)
        video_layout.addLayout(controls)
        compensation = QHBoxLayout()
        compensation.addWidget(QLabel("弹幕延迟补偿"))
        self.delay = QDoubleSpinBox()
        self.delay.setRange(-30, 60)
        self.delay.setSingleStep(.5)
        self.delay.setSuffix(" 秒")
        self.delay.setToolTip("仅调整本次回放，不改变原始弹幕和已封装字幕。弹幕早于画面时调大。")
        self.delay.valueChanged.connect(self._change_delay)
        compensation.addWidget(self.delay)
        compensation.addStretch()
        video_layout.addLayout(compensation)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索本场弹幕内容或用户名，点击结果跳转")
        self.search.setClearButtonEnabled(True)
        chat_layout.addWidget(self.search)
        self.model = EventModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.clicked.connect(lambda index: self.seek_event(self.model.visible[index.row()].offset))
        self.table.activated.connect(lambda index: self.seek_event(self.model.visible[index.row()].offset))
        chat_layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.follow = QCheckBox("弹幕列表跟随播放")
        self.follow.setChecked(True)
        self.count = QLabel("0 条")
        footer.addWidget(self.follow)
        footer.addStretch()
        footer.addWidget(self.count)
        chat_layout.addLayout(footer)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(150)
        self.filter_timer.timeout.connect(self._filter)
        self.search.textChanged.connect(lambda _text: self.filter_timer.start())
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([480, 620])
        outer.addWidget(split, 1)

    def _loaded(self, events, timeline, note):
        if self._closed:
            return
        self.timeline = timeline
        self.model.set_events(events)
        self._event_offsets = [e.offset for e in events]
        self._filter()
        self.delay.setValue(timeline.delay)
        self.status.setText(note or "点击弹幕可跳转；场内时间包含断线空档，进度条是当前分片时间。")
        self.segment.blockSignals(True)
        for index, part in enumerate(timeline.segments):
            self.segment.addItem("%d · %s" % (index + 1, part.path.name))
        self.segment.setCurrentIndex(-1)
        self.segment.blockSignals(False)
        self.segment.setEnabled(bool(timeline.segments))
        self.play_btn.setEnabled(bool(timeline.segments))
        if self._initial_seek is not None:
            self.seek_event(self._initial_seek)
        elif timeline.segments:
            self._choose_segment(0, autoplay=False)

    def _filter(self):
        self.model.filter(self.search.text())
        self.count.setText("%d / %d 条" % (len(self.model.visible), len(self.model.events)))

    def _choose_segment(self, index: int, position: float = 0, *, autoplay=True):
        if self._closed or not 0 <= index < len(self.timeline.segments):
            return
        part = self.timeline.segments[index]
        if not part.path.is_file():
            self.status.setText("分片已移动或被处理队列替换，请关闭后刷新录像库再打开。")
            return
        self.player.stop()
        self.segment_index = index
        self._pending_seek = max(0, int(position * 1000))
        self.segment.blockSignals(True)
        self.segment.setCurrentIndex(index)
        self.segment.blockSignals(False)
        self.player.setSource(QUrl.fromLocalFile(str(part.path.resolve())))
        if autoplay:
            self.player.play()

    def seek_event(self, offset: float):
        target = self.timeline.locate(offset)
        if target is None:
            self.status.setText("这条弹幕处于起录前、断线空档或视频范围之外，没有对应画面。")
            return
        index, seconds = target
        if index == self.segment_index:
            self._pending_seek = int(seconds * 1000)
            self._apply_pending_seek()
            self.player.play()
        else:
            self._choose_segment(index, seconds)

    def _apply_pending_seek(self):
        if self._pending_seek is not None and self.player.isSeekable():
            value, self._pending_seek = self._pending_seek, None
            self.player.setPosition(value)

    def _media_status(self, status):
        if self._closed:
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.player.setActiveSubtitleTrack(-1)
            self._apply_pending_seek()
        elif status == QMediaPlayer.EndOfMedia and self.segment_index + 1 < len(self.timeline.segments):
            self._choose_segment(self.segment_index + 1)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        elif self.segment_index < 0:
            self._choose_segment(0)
        else:
            self.player.play()

    def _change_delay(self, value):
        self.timeline.delay = value
        self._last_chat = None
        self._position_changed(self.player.position())

    def _position_changed(self, milliseconds):
        if self._closed or self.segment_index < 0:
            return
        if not self.slider.isSliderDown():
            self.slider.setValue(milliseconds)
        self.position.setText("%s / %s" % (clock(milliseconds / 1000), clock(self.player.duration() / 1000)))
        part = self.timeline.segments[self.segment_index]
        if part.start < 0:
            self.chat.setText("此分片的场内时间无法确定，暂不显示同步弹幕。")
            self.table.clearSelection()
            return
        offset = part.start + milliseconds / 1000 - self.timeline.delay
        index = bisect.bisect_right(self._event_offsets, offset) - 1
        if index != self._last_chat:
            self._last_chat = index
            recent = self.model.events[max(0, index - 4):index + 1] if index >= 0 else []
            self.chat.setText("\n".join((e.user + "：" + e.content)[:250] for e in recent) or "尚无弹幕")
        if self.follow.isChecked() and not self.search.text().strip():
            row = bisect.bisect_right(self.model.offsets, offset) - 1
            if row >= 0 and self.table.currentIndex().row() != row:
                self.table.selectRow(row)
                self.table.scrollTo(self.model.index(row, 0), QAbstractItemView.PositionAtCenter)
            elif row < 0:
                self.table.clearSelection()

    def _close_player(self):
        self._closed = True
        self._cancel.set()
        self.filter_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
