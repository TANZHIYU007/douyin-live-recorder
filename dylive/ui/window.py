"""主窗口：左侧监测列表，右侧选中房间的预览和弹幕。

线程模型：
  * 每个房间一个 Recorder 线程（manager.py），所有房间共用一个 Chromium；
  * 弹幕先进各房间自己的队列，由 UI 计时器按批取走 —— 高频房间每秒几十条，
    逐条发信号会把事件循环压垮；
  * 一次性结果（解析房间号、头像下载完）走 Qt 信号，跨线程由 Qt 排队投递。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import requests
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QTableView, QVBoxLayout,
                               QWidget, QFileDialog)

from .. import room as room_mod
from .. import paths, runtime, subtitle, video
from ..manager import IDLE, RECORDING, RoomManager
from ..preview import FOLLOW_SOURCE
from ..recorder import Options
from ..utils import UA, setup_console
from . import theme
from .first_run import ensure_runtime
from .preview_feed import PreviewFeed
from .settings import AppSettings, SettingsDialog
from .widgets import (Card, DanmakuModel, KindFilter, PreviewView, RoomHeader,
                      RoomRow, StatTile, set_pill)

log = logging.getLogger("ui")

APP_NAME = "拾光"
APP_TITLE = "拾光 · 抖音直播录制"

# 弹幕面板的显示档位，只影响看什么，不影响存什么
VIEW_FILTERS = [
    {"chat", "emoji"},
    {"chat", "emoji", "gift", "social", "fansclub"},
    None,
]

SPLIT_GAP = 14          # 分隔条宽度，同时也是左右两栏之间的视觉间距
TAB_BAR_HEIGHT = 31     # 标签栏高度，预览列顶上要留同样高度才能和它对齐
TAB_GAP = 8
MIN_TABLE_WIDTH = 430   # 弹幕表最窄也要这么宽，不然内容列没法看


class QueueLogHandler(logging.Handler):
    def __init__(self, limit: int = 5000):
        super().__init__()
        self.lines: deque = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s  %(name)-12s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:               # noqa: BLE001 - 日志失败不能拖垮录制
            return
        with self._lock:
            self.lines.append(line)

    def drain(self, limit: int = 200) -> List[str]:
        with self._lock:
            return [self.lines.popleft() for _ in range(min(limit, len(self.lines)))]


class Bridge(QObject):
    """一次性事件的信号出口，可以从任意线程 emit。"""

    room_resolved = Signal(str, object)     # web_rid, RoomInfo
    resolve_failed = Signal(str)
    avatar_ready = Signal(str, bytes)
    subtitle_log = Signal(str)
    subtitle_done = Signal(int, str)        # 成品数量, 错误信息


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = AppSettings.load()
        theme.set_mode(self.settings.theme)

        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 860)
        self.setMinimumSize(1080, 700)

        self.bridge = Bridge()
        self.log_handler = QueueLogHandler()
        logging.getLogger().addHandler(self.log_handler)

        self.manager = self._make_manager()
        self.selected: Optional[str] = None
        self.rows: Dict[str, RoomRow] = {}
        self.items: Dict[str, QListWidgetItem] = {}
        self.feeds: Dict[str, deque] = {}       # rid -> 界面这边留的弹幕历史
        self.avatars: Dict[str, bytes] = {}
        self.preview_feed = PreviewFeed(self)
        self._ffmpeg = ""

        self._build()
        self._connect()
        self._apply_view_filter()

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start(120)

        for rid in self.settings.rooms:
            self._add_room(rid, start=False)
        if self.settings.rooms:
            threading.Thread(target=self._refresh_saved_rooms,
                             args=(list(self.settings.rooms),),
                             name="ui-restore", daemon=True).start()
        self._log("%s 已就绪。输入直播间号或链接，点「添加」加入监测列表。" % APP_NAME)

    # -- 组装 -------------------------------------------------------------

    def _make_manager(self) -> RoomManager:
        login_dir = ""
        if self.settings.keep_login:
            login_dir = str(paths.app_data_dir() / "chrome-data")
        return RoomManager(self._make_options, headless=not self.settings.headful,
                           user_data_dir=login_dir)

    def _make_options(self, web_rid: str) -> Options:
        s = self.settings
        return Options(
            target=web_rid,
            out_dir=Path(s.out_dir),
            quality=s.quality,
            container=s.container,
            segment_seconds=s.segment_minutes * 60,
            record_video=s.record_video,
            record_danmaku=s.record_danmaku,
            kinds=tuple(s.kinds),
            write_xml=s.write_xml,
            show_console=False,
            headless=not s.headful,
            embed_subtitle=s.embed_subtitle,
            subtitle_replace=s.subtitle_replace,
            subtitle_size=s.subtitle_size,
            subtitle_duration=s.subtitle_duration,
            subtitle_reserve=s.subtitle_reserve,
            watch=True,                 # 监测列表天然就是守候模式
        )

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_topbar())

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(SPLIT_GAP)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([296, 1004])

        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 14)
        body_lay.addWidget(split)
        outer.addWidget(body, 1)
        self.setCentralWidget(root)

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topbar")
        bar.setFixedHeight(70)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)

        mark = QLabel("拾")
        mark.setObjectName("brandMark")
        mark.setFixedSize(38, 38)
        mark.setAlignment(Qt.AlignCenter)
        lay.addWidget(mark)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brandName")
        sub = QLabel("画面 + 弹幕同步录制")
        sub.setObjectName("brandSub")
        brand.addWidget(name)
        brand.addWidget(sub)
        lay.addLayout(brand)
        lay.addSpacing(18)

        self.input = QLineEdit()
        self.input.setPlaceholderText("直播间号 / https://live.douyin.com/… / v.douyin.com 短链")
        self.input.setMinimumWidth(280)
        self.input.setClearButtonEnabled(True)
        lay.addWidget(self.input, 1)

        self.add_btn = QPushButton("添加")
        self.add_btn.setObjectName("ghost")
        self.add_btn.setMinimumWidth(72)
        lay.addWidget(self.add_btn)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("iconBtn")
        self.theme_btn.setToolTip("切换深浅色")
        lay.addWidget(self.theme_btn)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghost")
        lay.addWidget(self.settings_btn)

        self.start_all_btn = QPushButton("全部开始")
        self.start_all_btn.setObjectName("primary")
        self.start_all_btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self.start_all_btn)

        self.stop_all_btn = QPushButton("全部停止")
        self.stop_all_btn.setObjectName("stopBtn")
        self.stop_all_btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self.stop_all_btn)
        return bar

    def _build_left(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("settingsHost")
        panel.setMinimumWidth(264)
        panel.setMaximumWidth(420)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("监测列表")
        title.setObjectName("cardTitle")
        self.count_label = QLabel("0 个房间")
        self.count_label.setObjectName("hint")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.count_label)
        lay.addLayout(head)

        self.room_list = QListWidget()
        self.room_list.setSpacing(0)
        self.room_list.setUniformItemSizes(False)
        self.room_list.setSelectionMode(QListWidget.SingleSelection)
        lay.addWidget(self.room_list, 1)

        self.toggle_btn = QPushButton("开始")
        self.remove_btn = QPushButton("移除")
        self.open_btn = QPushButton("打开目录")
        self.subtitle_btn = QPushButton("补做弹幕版…")
        self.subtitle_btn.setToolTip(
            "挑一场历史录像的 .jsonl，把弹幕封装成它的字幕轨（不重新编码）")
        for b in (self.toggle_btn, self.remove_btn, self.open_btn, self.subtitle_btn):
            b.setObjectName("ghost")
        for pair in ((self.toggle_btn, self.remove_btn),
                     (self.open_btn, self.subtitle_btn)):
            row = QHBoxLayout()
            row.setSpacing(8)
            for b in pair:
                row.addWidget(b, 1)
            lay.addLayout(row)

        self.summary = QLabel("—")
        self.summary.setObjectName("hint")
        lay.addWidget(self.summary)
        return panel

    def _build_right(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        head_card = Card()
        self.header = RoomHeader()
        head_card.body.addWidget(self.header)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.tile_time = StatTile("录制时长", "00:00:00")
        self.tile_size = StatTile("已写入", "0 MB")
        self.tile_chat = StatTile("弹幕", "0", accent="cyan")
        self.tile_gift = StatTile("礼物 / 关注", "0", accent="accent")
        for i, tile in enumerate((self.tile_time, self.tile_size,
                                  self.tile_chat, self.tile_gift)):
            grid.addWidget(tile, 0, i)
        head_card.body.addLayout(grid)
        lay.addWidget(head_card)

        # 画面自己会在内部按比例居中，所以直接扔进 splitter 让它铺满就行。
        # 之前套了一层带 AlignHCenter 的容器，结果布局只给它 sizeHint 那么宽，
        # 预览永远缩在最小尺寸。
        self.preview_view = PreviewView()

        tabs = QTabWidget()
        self.model = DanmakuModel()
        self.filter = KindFilter()
        self.filter.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.filter)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # 列宽用字体度量算，别拍脑袋写死 —— 换字体或换 DPI 时间就会被截成 00:01…
        fm = self.table.fontMetrics()
        # +N 是留给 QSS 的 8px 内边距（左右各一份）和委托自己的边距，
        # 少留了时间就会被省略成 00:01…
        widths = (fm.horizontalAdvance("00:00:00") + 30,
                  fm.horizontalAdvance("粉丝团") + 26,
                  fm.horizontalAdvance("用户15838559") + 18)
        for col, width, mode in ((0, widths[0], QHeaderView.Fixed),
                                 (1, widths[1], QHeaderView.Fixed),
                                 (2, widths[2], QHeaderView.Interactive)):
            self.table.setColumnWidth(col, width)
            header.setSectionResizeMode(col, mode)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setMinimumSectionSize(40)

        self.view_filter = QComboBox()
        self.view_filter.addItems(["只看聊天", "聊天 + 礼物关注", "全部事件"])
        self.view_filter.setCurrentIndex(1)
        self.view_filter.setFixedWidth(150)
        self.feed_count = QLabel("0 条")
        self.feed_count.setObjectName("hint")
        filter_row = QWidget()
        filter_lay = QHBoxLayout(filter_row)
        filter_lay.setContentsMargins(4, 0, 2, 0)
        filter_lay.setSpacing(8)
        filter_lay.addWidget(QLabel("显示"))
        filter_lay.addWidget(self.view_filter)
        filter_lay.addStretch(1)
        filter_lay.addWidget(self.feed_count)

        wrapper = QFrame()
        wrapper.setObjectName("card")
        wrap_lay = QVBoxLayout(wrapper)
        wrap_lay.setContentsMargins(6, 8, 6, 6)
        wrap_lay.setSpacing(8)
        wrap_lay.addWidget(filter_row)
        wrap_lay.addWidget(self.table, 1)
        tabs.addTab(wrapper, "弹幕")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        log_wrap = QFrame()
        log_wrap.setObjectName("card")
        log_lay = QVBoxLayout(log_wrap)
        log_lay.setContentsMargins(6, 6, 6, 6)
        log_lay.addWidget(self.log_view)
        tabs.addTab(log_wrap, "运行日志")
        # 竖屏预览放左边、弹幕放右边，比上下叠更省地方；中缝可以拖。
        # 预览外面套一列：顶上放个和右侧标签栏等高的标题，两边顶边就对齐了；
        # 预览自己按画面比例决定高度，剩下的交给弹性空白，不留黑边。
        preview_col = QWidget()
        col = QVBoxLayout(preview_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(TAB_GAP)
        col_title = QLabel("实时画面")
        col_title.setObjectName("cardTitle")
        col_title.setFixedHeight(TAB_BAR_HEIGHT)
        col.addWidget(col_title)
        col.addWidget(self.preview_view)

        # 画面按比例收完高度后，这一列下面会空一大片。放本场产出的文件正好
        # ——既填上了，也是用户真想知道的东西。
        files_card = Card("本场文件")
        self.files_list = QListWidget()
        self.files_list.setObjectName("filesList")
        self.files_list.setSelectionMode(QListWidget.NoSelection)
        self.files_list.setFocusPolicy(Qt.NoFocus)
        self.files_list.setMinimumHeight(60)
        # 录像文件名很长，不关掉横向滚动条会撑出一条丑陋的滚动条
        self.files_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.files_list.setTextElideMode(Qt.ElideRight)
        self.files_list.setWordWrap(False)
        files_card.body.addWidget(self.files_list)
        col.addWidget(files_card, 1)
        self._files_shown: List[str] = []

        lower = QSplitter(Qt.Horizontal)
        lower.setChildrenCollapsible(False)
        lower.setHandleWidth(SPLIT_GAP)
        lower.addWidget(preview_col)
        lower.addWidget(tabs)
        self.lower_split = lower
        self._preview_aspect = 0.0
        lower.setStretchFactor(0, 0)
        lower.setStretchFactor(1, 1)
        lower.setSizes([264, 776])
        lay.addWidget(lower, 1)
        return panel

    def _connect(self) -> None:
        self.add_btn.clicked.connect(self._on_add)
        self.input.returnPressed.connect(self._on_add)
        self.settings_btn.clicked.connect(self._open_settings)
        self.theme_btn.clicked.connect(self._toggle_theme)
        self.start_all_btn.clicked.connect(self._start_all)
        self.stop_all_btn.clicked.connect(self._stop_all)
        self.toggle_btn.clicked.connect(self._toggle_selected)
        self.remove_btn.clicked.connect(self._remove_selected)
        self.open_btn.clicked.connect(self._open_folder)
        self.subtitle_btn.clicked.connect(self._embed_subtitles)
        self.bridge.subtitle_log.connect(self._log)
        self.bridge.subtitle_done.connect(self._on_subtitle_done)
        self.preview_feed.frame.connect(self._on_preview_frame)
        self.room_list.currentItemChanged.connect(self._on_selection)
        self.view_filter.currentIndexChanged.connect(self._apply_view_filter)
        self.bridge.room_resolved.connect(self._on_room_resolved)
        self.bridge.resolve_failed.connect(self._on_resolve_failed)
        self.bridge.avatar_ready.connect(self._on_avatar)
        self._refresh_theme_button()

    # -- 房间增删 ---------------------------------------------------------

    def _on_add(self) -> None:
        target = self.input.text().strip()
        if not target:
            return
        self.add_btn.setEnabled(False)
        self.add_btn.setText("解析中")
        threading.Thread(target=self._resolve_worker, args=(target,),
                         name="ui-resolve", daemon=True).start()

    def _resolve_worker(self, target: str) -> None:
        try:
            sess = room_mod.make_session()
            rid = room_mod.parse_target(target, sess)
            info = room_mod.fetch(rid, sess)
        except Exception as exc:            # noqa: BLE001 - 报给界面
            self.bridge.resolve_failed.emit(str(exc))
            return
        self.bridge.room_resolved.emit(rid, info)

    def _on_room_resolved(self, rid: str, info) -> None:
        known = rid in self.rows
        if not known:                       # 只有手动添加才需要复位输入区
            self.add_btn.setEnabled(True)
            self.add_btn.setText("添加")
            self.input.clear()
        self._add_room(rid, info=info, start=not known)
        if known:
            return
        self._log("已添加房间 %s（%s）" % (rid, info.nickname or "未知"))

    def _on_resolve_failed(self, message: str) -> None:
        self.add_btn.setEnabled(True)
        self.add_btn.setText("添加")
        self._log("添加失败：%s" % message)
        QMessageBox.warning(self, APP_NAME, "解析直播间失败：\n%s" % message)

    def _add_room(self, rid: str, info=None, start: bool = False) -> None:
        entry = self.manager.add(rid)
        if info is not None:
            entry.info = info
            if info.avatar and rid not in self.avatars:
                threading.Thread(target=self._avatar_worker, args=(rid, info.avatar),
                                 name="ui-avatar", daemon=True).start()

        if rid in self.rows:
            # 已经在列表里（多半是配置恢复的），只补信息，别再建一行
            if rid == self.selected and entry.info is not None:
                self.header.show_room(entry.info, entry.state)
            if start:
                entry.start()
            return

        row = RoomRow()
        item = QListWidgetItem(self.room_list)
        item.setData(Qt.UserRole, rid)
        item.setSizeHint(row.sizeHint())
        self.room_list.addItem(item)
        self.room_list.setItemWidget(item, row)
        self.rows[rid] = row
        self.items[rid] = item
        self.feeds[rid] = deque(maxlen=3000)

        if self.selected is None:
            self._select(rid)
        self._persist_rooms()
        self._refresh_counts()
        if start:
            entry.start()

    def _refresh_saved_rooms(self, rids: List[str]) -> None:
        """配置里恢复的房间只有房间号，后台补一次昵称/头像/开播状态。"""
        sess = room_mod.make_session()
        for rid in rids:
            try:
                info = room_mod.fetch(rid, sess)
            except Exception:               # noqa: BLE001 - 补信息失败无所谓
                continue
            if info.ok:
                self.bridge.room_resolved.emit(rid, info)

    def _avatar_worker(self, rid: str, url: str) -> None:
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            resp.raise_for_status()
        except requests.RequestException:
            return
        self.bridge.avatar_ready.emit(rid, resp.content)

    def _on_avatar(self, rid: str, data: bytes) -> None:
        self.avatars[rid] = data
        row = self.rows.get(rid)
        if row is not None:
            row.set_avatar(data)
        if rid == self.selected:
            self.header.set_avatar(data)

    def _remove_selected(self) -> None:
        rid = self.selected
        if not rid:
            return
        entry = self.manager.get(rid)
        if entry is not None and entry.active:
            answer = QMessageBox.question(
                self, APP_NAME, "这个房间还在录，确定移除吗？（会先正常收尾）")
            if answer != QMessageBox.Yes:
                return
        self.manager.remove(rid)
        item = self.items.pop(rid, None)
        if item is not None:
            self.room_list.takeItem(self.room_list.row(item))
        self.rows.pop(rid, None)
        self.feeds.pop(rid, None)
        self.avatars.pop(rid, None)
        if self.preview_feed.rid == rid:
            self._stop_preview()
        self.selected = None
        self._persist_rooms()
        self._refresh_counts()
        self._log("已移除房间 %s" % rid)
        if self.room_list.count():
            self.room_list.setCurrentRow(0)
        else:
            self.header.show_empty()
            self.model.clear()

    def _persist_rooms(self) -> None:
        self.settings.rooms = [r.web_rid for r in self.manager.rooms()]
        self.settings.save()

    # -- 选择与启停 -------------------------------------------------------

    def _select(self, rid: str) -> None:
        item = self.items.get(rid)
        if item is not None:
            self.room_list.setCurrentItem(item)

    def _on_selection(self, current, _previous) -> None:
        rid = current.data(Qt.UserRole) if current is not None else None
        self.selected = rid
        self._stop_preview()
        if rid is None:
            self.header.show_empty()
            self.model.clear()
            return

        entry = self.manager.get(rid)
        if entry is not None and entry.info is not None:
            self.header.show_room(entry.info, entry.state)
        else:
            self.header.show_empty()
            self.header.nickname.setText(rid)
        data = self.avatars.get(rid)
        if data:
            self.header.set_avatar(data)
        self.model.replace(list(self.feeds.get(rid, ())))
        self._update_feed_count()
        self.table.scrollToBottom()

    def _toggle_selected(self) -> None:
        entry = self.manager.get(self.selected or "")
        if entry is None:
            return
        if entry.active:
            entry.stop()
            self._log("正在停止 %s…" % entry.display_name())
        else:
            entry.start()
            self._log("开始监测 %s" % entry.display_name())

    def _start_all(self) -> None:
        started = self.manager.start_all()
        self._log("已启动 %d 个房间的监测。" % started if started else "所有房间都已在运行。")

    def _stop_all(self) -> None:
        self.manager.stop_all()
        self._log("正在停止全部房间，等 ffmpeg 收尾…")

    # -- 设置与主题 -------------------------------------------------------

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != SettingsDialog.Accepted:
            return
        before = (self.settings.headful, self.settings.keep_login)
        before_pv = (self.settings.preview_width, self.settings.preview_fps)
        dialog.apply_to(self.settings)
        self.settings.save()
        self._log("设置已保存。")

        if before != (self.settings.headful, self.settings.keep_login):
            if self.manager.active_count:
                self._log("浏览器选项已改，停止并重新开始后生效。")
            else:
                self.manager.hub.stop()
                self.manager = self._make_manager()
                for rid in list(self.rows):
                    self.manager.add(rid)
                self._log("浏览器选项已生效。")
        if not self.settings.preview:
            self._stop_preview()
        elif before_pv != (self.settings.preview_width, self.settings.preview_fps):
            # 宽度和帧率是 ffmpeg 的启动参数，只能重开一路才生效
            self._stop_preview()
            self.preview_view.clear_frame("正在按新设置重连…")

    def _toggle_theme(self) -> None:
        mode = "dark" if theme.current() == "light" else "light"
        self.settings.theme = mode
        self.settings.save()
        self._apply_theme(mode)

    def _apply_theme(self, mode: str) -> None:
        theme.set_mode(mode)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet(mode))
        self.model.restyle()
        self.header._reset_avatar()
        data = self.avatars.get(self.selected or "")
        if data:
            self.header.set_avatar(data)
        for rid, row in self.rows.items():
            row._reset_avatar()
            if rid in self.avatars:
                row.set_avatar(self.avatars[rid])
        self._refresh_theme_button()

    def _refresh_theme_button(self) -> None:
        self.theme_btn.setText("🌙" if theme.current() == "light" else "☀")

    # -- 预览 -------------------------------------------------------------

    def _ensure_ffmpeg(self) -> str:
        if not self._ffmpeg:
            try:
                self._ffmpeg = video.find_ffmpeg()
            except RuntimeError as exc:
                self._log("找不到 ffmpeg：%s" % exc)
        return self._ffmpeg

    def _sync_preview(self, rid: str, entry) -> None:
        """预览只跟着当前选中的房间跑，切走就停，省资源。"""
        want = (self.settings.preview and entry is not None
                and entry.state == RECORDING and entry.info is not None
                and entry.info.living)
        if not want:
            self._stop_preview()
            # 提示语每次都刷新：只在「刚停掉一路流」时才改的话，切到一个
            # 没在录的房间会留着上一个房间的「正在连接画面…」，看着像卡住了
            self.preview_view.clear_frame(
                "该房间未在录制" if entry is not None else
                "选中正在录制的房间即可预览")
            return
        if self.preview_feed.rid == rid and self.preview_feed.alive:
            return

        self._stop_preview()
        ffmpeg = self._ensure_ffmpeg()
        # 预览刻意拿最低画质：几百 kbps，解码开销可以忽略
        url = entry.info.pick("标清", "flv")
        if not ffmpeg or not url:
            return
        self._sync_preview_target()
        self.preview_feed.start(ffmpeg, url, rid,
                                width=self.settings.preview_width,
                                fps=self.settings.preview_fps)
        self.preview_view.clear_frame("正在连接画面…")

    def _sync_preview_target(self) -> None:
        """告诉解码线程画面最终要多宽，缩放就在它那边顺手做掉。

        算的是物理像素（乘 devicePixelRatio），高分屏上才不会先缩小再放大。
        """
        ratio = self.preview_view.devicePixelRatioF() or 1.0
        width = max(self.preview_view.width(), 240)
        self.preview_feed.set_target_width(int(width * ratio))

    def _on_preview_frame(self, img) -> None:
        """新帧到了就画一次 —— 节奏跟着直播源走，不再受定时器牵制。"""
        self.preview_view.show_image(img)
        self._fit_preview_column()

    def _fit_preview_column(self) -> None:
        """按画面比例给预览列分一个合适的宽度。

        横屏（游戏直播）和竖屏（手机直播）差别太大：同样 264px 宽，竖屏高
        470px 刚好，横屏只有 148px，小得看不清。所以让列宽跟着比例走。

        只在比例真的变了才调一次 —— 不是每帧都调，否则用户拖过的分割线会
        被一直覆盖回去。
        """
        aspect = self.preview_view._aspect
        if abs(aspect - self._preview_aspect) < 0.01:
            return
        self._preview_aspect = aspect

        total = self.lower_split.width()
        avail_h = max(120, self.lower_split.height() - TAB_BAR_HEIGHT - TAB_GAP)
        want = int(avail_h * 0.72 * aspect)
        # 上限有两条：不超过右侧一半，且必须给弹幕表留够宽度 —— 否则窄窗口下
        # 横屏画面会把「内容」列挤到只剩百来像素，弹幕全被省略号吃掉
        want = min(want, int(total * 0.45), total - MIN_TABLE_WIDTH - SPLIT_GAP)
        want = max(200, want)
        self.lower_split.setSizes([want, max(200, total - want - SPLIT_GAP)])

    def _stop_preview(self) -> None:
        self.preview_feed.stop()

    # -- 定时刷新 ---------------------------------------------------------

    def _on_tick(self) -> None:
        for line in self.log_handler.drain():
            self.log_view.appendPlainText(line)

        # 所有房间的弹幕都要收，不然切回去就断档了
        for entry in self.manager.rooms():
            events = entry.buffer.drain()
            if not events:
                continue
            feed = self.feeds.setdefault(entry.web_rid, deque(maxlen=3000))
            feed.extend(events)
            if entry.web_rid == self.selected:
                bar = self.table.verticalScrollBar()
                at_bottom = bar.value() >= bar.maximum() - 4
                self.model.extend(events)
                self._update_feed_count()
                if at_bottom:
                    self.table.scrollToBottom()

        self._refresh_rows()

        entry = self.manager.get(self.selected or "")
        self._refresh_detail(entry)
        if self.selected:
            self._sync_preview(self.selected, entry)
        # 画面不在这里取 —— 定时器和直播源的帧间隔对不上，画面节奏会忽快忽
        # 慢，而且同一张图会被反复解码。改由 PreviewFeed 按帧推过来。

    def _refresh_rows(self) -> None:
        recording = 0
        total = 0
        for entry in self.manager.rooms():
            row = self.rows.get(entry.web_rid)
            if row is None:
                continue
            st = entry.status()
            state = entry.state
            if state == RECORDING:
                recording += 1
            total += st.total_bytes

            if entry.info is not None and not row.name.text().strip("—"):
                pass
            bits = []
            if state == RECORDING:
                secs = int(st.elapsed)
                bits.append("%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60,
                                                secs % 60))
                bits.append(_human(st.total_bytes))
            elif st.post:
                bits.append(st.post[:30])
            elif entry.error:
                bits.append(entry.error[:28])
            else:
                bits.append("房间号 " + entry.web_rid)
            chats = st.counts.get("chat", 0) + st.counts.get("emoji", 0)
            if chats:
                bits.append("💬 %d" % chats)
            row.update_row(entry.display_name(), state, "  ·  ".join(bits))

        self.count_label.setText("%d 个房间" % len(self.rows))
        self.summary.setText("录制中 %d / %d  ·  本次共写入 %s"
                             % (recording, len(self.rows), _human(total)))
        entry = self.manager.get(self.selected or "")
        self.toggle_btn.setText("停止" if (entry and entry.active) else "开始")
        self.toggle_btn.setEnabled(entry is not None)
        self.remove_btn.setEnabled(entry is not None)

    def _refresh_counts(self) -> None:
        self.count_label.setText("%d 个房间" % len(self.rows))

    def _refresh_files(self, files) -> None:
        """只在列表内容真的变了才重建，否则每 120ms 刷一次会闪。"""
        lines = ["%s|%d" % (name, size) for name, size in files]
        if lines == self._files_shown:
            return
        self._files_shown = lines
        self.files_list.clear()
        for (name, size) in files:
            # 大小放前面：名字被省略号截掉无所谓，大小不能看不见
            item = QListWidgetItem("%-9s  %s" % (_human(size), name))
            item.setToolTip(name)
            self.files_list.addItem(item)
        if not files:
            item = QListWidgetItem("还没有产出文件")
            item.setForeground(QColor(theme.palette()["muted"]))
            self.files_list.addItem(item)

    def _refresh_detail(self, entry) -> None:
        if entry is None:
            self.tile_time.set_value("00:00:00")
            self.tile_size.set_value("0 MB")
            self.tile_chat.set_value("0")
            self.tile_gift.set_value("0")
            self._refresh_files([])
            return
        st = entry.status()
        self._refresh_files(st.files)
        if st.info is not None and st.info is not entry.info:
            entry.info = st.info
        if entry.info is not None:
            self.header.show_room(entry.info, entry.state)
        else:
            set_pill(self.header.pill, entry.state)

        secs = int(st.elapsed)
        self.tile_time.set_value("%02d:%02d:%02d"
                                 % (secs // 3600, secs % 3600 // 60, secs % 60))
        self.tile_size.set_value(_human(st.total_bytes))
        self.tile_chat.set_value(str(st.counts.get("chat", 0)
                                     + st.counts.get("emoji", 0)))
        self.tile_gift.set_value(str(st.counts.get("gift", 0)
                                     + st.counts.get("social", 0)))

    def _apply_view_filter(self) -> None:
        self.filter.set_kinds(VIEW_FILTERS[self.view_filter.currentIndex()])
        self._update_feed_count()
        self.table.scrollToBottom()

    def _update_feed_count(self) -> None:
        shown, total = self.filter.rowCount(), self.model.rowCount()
        self.feed_count.setText("%d 条" % total if shown == total
                                else "%d / %d 条" % (shown, total))

    # -- 杂项 -------------------------------------------------------------

    def _embed_subtitles(self) -> None:
        """挑一场历史录像的弹幕文件，把它封装成同名视频的字幕轨。"""
        picked, _ = QFileDialog.getOpenFileName(
            self, "选择要补做弹幕版的录像（选它的 .jsonl 弹幕文件）",
            self.settings.out_dir, "弹幕记录 (*.jsonl)")
        if not picked:
            return
        jsonl = Path(picked)
        videos = subtitle.find_videos(jsonl.with_suffix(""))
        if not videos:
            QMessageBox.warning(self, APP_NAME,
                                "同目录下没找到对应的视频文件：\n%s*" % jsonl.stem)
            return
        ffmpeg = self._ensure_ffmpeg()
        if not ffmpeg:
            QMessageBox.warning(self, APP_NAME, "找不到 ffmpeg，无法封装。")
            return

        self.subtitle_btn.setEnabled(False)
        self.subtitle_btn.setText("处理中…")
        self._log("开始补做弹幕版：%s（%d 个片段）" % (jsonl.stem, len(videos)))
        threading.Thread(target=self._subtitle_worker, args=(ffmpeg, jsonl, videos),
                         name="ui-subtitle", daemon=True).start()

    def _subtitle_worker(self, ffmpeg: str, jsonl: Path, videos: List[Path]) -> None:
        s = self.settings
        try:
            results = subtitle.process(
                ffmpeg, jsonl, videos,
                style=subtitle.Style(size=s.subtitle_size,
                                     duration=s.subtitle_duration,
                                     reserve=s.subtitle_reserve),
                replace=s.subtitle_replace,
                progress=self.bridge.subtitle_log.emit)
        except Exception as exc:            # noqa: BLE001 - 报给界面
            self.bridge.subtitle_done.emit(0, str(exc))
            return
        self.bridge.subtitle_done.emit(len(results), "")

    def _on_subtitle_done(self, count: int, error: str) -> None:
        self.subtitle_btn.setEnabled(True)
        self.subtitle_btn.setText("补做弹幕版…")
        if error:
            self._log("补做失败：%s" % error)
            QMessageBox.critical(self, APP_NAME, "补做弹幕版失败：\n%s" % error)
        elif count:
            self._log("补做完成，生成 %d 个带弹幕的文件。" % count)
        else:
            self._log("没有生成文件（可能这场没有聊天弹幕）。")

    def _open_folder(self) -> None:
        path = Path(self.settings.out_dir)
        path.mkdir(parents=True, exist_ok=True)
        paths.open_in_file_manager(path)

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 窗口变大了就该多解一点像素出来，变小了少解一点
        self._sync_preview_target()

    def closeEvent(self, event) -> None:
        if self.manager.active_count:
            answer = QMessageBox.question(
                self, APP_NAME,
                "还有 %d 个房间在运行，确定退出吗？\n（会先让 ffmpeg 正常收尾）"
                % self.manager.active_count)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._stop_preview()
        self.settings.save()
        self.manager.shutdown(timeout=120)   # 可能正在封装字幕
        logging.getLogger().removeHandler(self.log_handler)
        event.accept()


def _human(num: int) -> str:
    if num >= 1 << 30:
        return "%.2f GB" % (num / (1 << 30))
    return "%.1f MB" % (num / (1 << 20))


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()],
                        force=True)
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)

    saved = AppSettings.load()
    theme.set_mode(saved.theme)
    app.setStyleSheet(theme.stylesheet())
    app.setFont(QFont(paths.ui_font(), 9))

    error = ensure_runtime()
    if error:
        QMessageBox.critical(None, APP_NAME, "运行环境准备失败：\n%s" % error)
        return 1
    runtime.apply_env()

    window = MainWindow()
    window.show()
    return app.exec()
