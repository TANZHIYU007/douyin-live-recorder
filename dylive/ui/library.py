"""录像库与录后处理队列窗口。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFrame, QHeaderView,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QTabWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import paths, subtitle
from ..library import RecordingSession, recording_prefix, scan_library
from ..processing import (COMPLETED, FAILED, PENDING, RUNNING, PostProcessQueue,
                          ProcessingTask)
from ..utils import human_size


class _Bridge(QObject):
    scanned = Signal(object)


class RecordingLibraryDialog(QDialog):
    def __init__(self, settings, queue: PostProcessQueue, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.queue = queue
        self.sessions: dict[str, RecordingSession] = {}
        self._scanning = False
        self._rescan_requested = False
        self._closed = False
        self._finished_tasks = self._finished_versions(queue.snapshot())
        self._bridge = _Bridge(self)
        self._bridge.scanned.connect(self._on_scanned)

        self.setWindowTitle("录像库与处理队列")
        self.resize(1040, 680)
        self.setMinimumSize(760, 480)
        self._build()
        self._connect()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_queue)
        self.timer.start(800)
        self.finished.connect(self._on_closed)
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("本地录像库")
        title.setObjectName("nickname")
        self.root_hint = QLabel(str(self.settings.out_dir))
        self.root_hint.setObjectName("hint")
        self.root_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(title)
        top.addWidget(self.root_hint, 1)
        self.open_root_btn = QPushButton("打开目录")
        self.open_root_btn.setObjectName("ghost")
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("ghost")
        top.addWidget(self.open_root_btn)
        top.addWidget(self.refresh_btn)
        outer.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_library_tab(), "录像库")
        self.tabs.addTab(self._build_queue_tab(), "处理队列")
        outer.addWidget(self.tabs, 1)

    def _build_library_tab(self) -> QWidget:
        page = QFrame()
        page.setObjectName("card")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索主播、标题或文件名")
        self.search.setClearButtonEnabled(True)
        self.count = QLabel("正在扫描…")
        self.count.setObjectName("hint")
        tools.addWidget(self.search, 1)
        tools.addWidget(self.count)
        lay.addLayout(tools)

        self.library_table = QTableWidget(0, 6)
        self.library_table.setHorizontalHeaderLabels(
            ["录制时间", "主播", "标题", "文件", "总大小", "处理状态"])
        self._prepare_table(self.library_table)
        header = self.library_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        lay.addWidget(self.library_table, 1)

        actions = QHBoxLayout()
        self.play_btn = QPushButton("播放")
        self.open_session_btn = QPushButton("打开所在目录")
        self.queue_btn = QPushButton("加入字幕处理队列")
        self.queue_btn.setObjectName("primary")
        actions.addWidget(self.play_btn)
        actions.addWidget(self.open_session_btn)
        actions.addStretch(1)
        actions.addWidget(self.queue_btn)
        lay.addLayout(actions)
        return page

    def _build_queue_tab(self) -> QWidget:
        page = QFrame()
        page.setObjectName("card")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        note = QLabel("任务按顺序逐个处理；失败任务会保留，重新打开拾光后仍可重试。")
        note.setObjectName("hint")
        lay.addWidget(note)

        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(
            ["加入时间", "录像", "状态", "进度", "尝试次数"])
        self._prepare_table(self.queue_table)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(self.queue_table, 1)

        actions = QHBoxLayout()
        self.retry_btn = QPushButton("重试失败任务")
        self.clear_btn = QPushButton("清除已完成记录")
        actions.addWidget(self.retry_btn)
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)
        lay.addLayout(actions)
        return page

    @staticmethod
    def _prepare_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setShowGrid(False)

    def _connect(self) -> None:
        self.refresh_btn.clicked.connect(self.refresh)
        self.open_root_btn.clicked.connect(self._open_root)
        self.search.textChanged.connect(self._render_sessions)
        self.library_table.itemSelectionChanged.connect(self._sync_buttons)
        self.library_table.itemDoubleClicked.connect(lambda _item: self._play())
        self.play_btn.clicked.connect(self._play)
        self.open_session_btn.clicked.connect(self._open_session)
        self.queue_btn.clicked.connect(self._enqueue)
        self.retry_btn.clicked.connect(self._retry)
        self.clear_btn.clicked.connect(self._clear_finished)

    def refresh(self) -> None:
        if self._closed:
            return
        if self._scanning:
            self._rescan_requested = True
            return
        self._scanning = True
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("扫描中…")
        root = Path(self.settings.out_dir)
        threading.Thread(target=lambda: self._bridge.scanned.emit(scan_library(root)),
                         name="library-scan", daemon=True).start()

    def _on_scanned(self, sessions: list[RecordingSession]) -> None:
        self._scanning = False
        if self._closed:
            return
        self.sessions = {session.key: session for session in sessions}
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("刷新")
        self._render_sessions()
        # 扫描过程中完成的任务可能不在这份快照里，需要再扫一次。
        if self._rescan_requested:
            self._rescan_requested = False
            self.refresh()
        self._refresh_queue()

    def _on_closed(self) -> None:
        self._closed = True
        self.timer.stop()

    def _render_sessions(self) -> None:
        selected_row = self.library_table.currentRow()
        selected_item = self.library_table.item(selected_row, 0) if selected_row >= 0 else None
        selected_key = selected_item.data(Qt.UserRole) if selected_item else None
        query = self.search.text().strip().casefold()
        rows = [session for session in self.sessions.values()
                if not query or query in ("%s %s %s" % (
                    session.streamer, session.title, session.prefix.name)).casefold()]
        self.library_table.setRowCount(len(rows))
        for row, session in enumerate(rows):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.recorded_at))
            file_text = "%d 视频" % len(session.videos)
            if session.jsonl:
                file_text += " · 弹幕"
            values = [when, session.streamer, session.title, file_text,
                      human_size(session.total_bytes), self._session_status(session)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, session.key)
                item.setToolTip(session.prefix.name if col in (0, 2) else value)
                self.library_table.setItem(row, col, item)
        self.count.setText("%d 场录像" % len(rows))
        if rows:
            self.library_table.selectRow(next((i for i, session in enumerate(rows)
                                               if session.key == selected_key), 0))
        self._sync_buttons()

    def _session_status(self, session: RecordingSession) -> str:
        task = self.queue.latest_for(session.prefix)
        if task:
            return {
                PENDING: "等待处理",
                RUNNING: "处理中",
                COMPLETED: "处理完成",
                FAILED: "处理失败",
            }.get(task.status, task.status)
        if not session.videos:
            return "没有视频"
        if not session.jsonl:
            return "没有弹幕记录"
        return "可处理"

    def _selected_session(self) -> RecordingSession | None:
        row = self.library_table.currentRow()
        item = self.library_table.item(row, 0) if row >= 0 else None
        return self.sessions.get(item.data(Qt.UserRole)) if item else None

    def _sync_buttons(self) -> None:
        session = self._selected_session()
        self.play_btn.setEnabled(bool(session and session.videos))
        self.open_session_btn.setEnabled(session is not None)
        self.queue_btn.setEnabled(bool(session and session.jsonl and session.videos))

    def _open_root(self) -> None:
        root = Path(self.settings.out_dir)
        root.mkdir(parents=True, exist_ok=True)
        paths.open_in_file_manager(root)

    def _open_session(self) -> None:
        session = self._selected_session()
        if session:
            paths.open_in_file_manager(session.folder)

    def _play(self) -> None:
        session = self._selected_session()
        if not session or not session.videos:
            return
        # 文件可能刚被后台替换，下一轮扫描尚未送达；播放前按当前目录重新找。
        videos = subtitle.find_videos(session.prefix) + list(session.videos)
        current = next((path for path in videos
                        if recording_prefix(path) == session.prefix and path.is_file()), None)
        if current is None:
            self.refresh()
            QMessageBox.information(self, "拾光", "录像文件已移动或删除，正在刷新录像库。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(current.resolve())))

    def _enqueue(self) -> None:
        session = self._selected_session()
        if not session or session.jsonl is None:
            return
        videos = subtitle.find_videos(session.prefix)
        try:
            task = self.queue.enqueue_subtitles(
                session.prefix,
                session.jsonl,
                videos,
                style=subtitle.Style.from_options(self.settings),
                replace=self.settings.subtitle_replace,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "拾光", str(exc))
            return
        self.tabs.setCurrentIndex(1)
        self._refresh_queue(select_id=task.id)

    def _refresh_queue(self, select_id: str = "") -> None:
        if self._closed:
            return
        tasks = list(reversed(self.queue.snapshot()))
        finished = self._finished_versions(tasks)
        files_changed = any(self._finished_tasks.get(key) != value
                            for key, value in finished.items())
        self._finished_tasks = finished
        if files_changed:
            self.refresh()
        selected = select_id or self._selected_task_id()
        self.queue_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = self._task_values(task)
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, task.id)
                if task.error:
                    item.setToolTip(task.error)
                self.queue_table.setItem(row, col, item)
            if task.id == selected:
                self.queue_table.selectRow(row)

        # 队列状态变了，录像库最后一列也跟着变，不用用户手动刷新。
        for row in range(self.library_table.rowCount()):
            item = self.library_table.item(row, 0)
            session = self.sessions.get(item.data(Qt.UserRole)) if item else None
            if session:
                self.library_table.item(row, 5).setText(self._session_status(session))

    @staticmethod
    def _finished_versions(tasks: list[ProcessingTask]) -> dict[str, tuple]:
        return {task.id: (task.status, task.attempts, task.updated_at)
                for task in tasks if task.status in {COMPLETED, FAILED}}

    @staticmethod
    def _task_values(task: ProcessingTask) -> list[str]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(task.created_at))
        name = Path(task.session_prefix).name
        state = {PENDING: "等待", RUNNING: "处理中", COMPLETED: "完成",
                 FAILED: "失败"}.get(task.status, task.status)
        progress = task.error if task.status == FAILED else task.message
        return [when, name, state, progress, str(task.attempts)]

    def _selected_task_id(self) -> str:
        row = self.queue_table.currentRow()
        item = self.queue_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.UserRole)) if item else ""

    def _retry(self) -> None:
        task_id = self._selected_task_id()
        if not task_id or not self.queue.retry(task_id):
            QMessageBox.information(self, "拾光", "请选择一个处理失败的任务。")
            return
        self._refresh_queue(select_id=task_id)

    def _clear_finished(self) -> None:
        removed = self.queue.clear_finished()
        self._refresh_queue()
        if not removed:
            QMessageBox.information(self, "拾光", "目前没有可以清除的已完成记录。")
