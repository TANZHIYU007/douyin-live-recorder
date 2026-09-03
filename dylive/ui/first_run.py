"""首次运行的准备界面。

打包版第一次启动要把内置的 Chromium 和 ffmpeg 从 exe 尾部解压到用户目录，
几百 MB 要花几十秒。没有反馈的话用户会以为程序卡死了。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QVBoxLayout)

from .. import runtime
from . import theme


class _Reporter(QObject):
    progress = Signal(int, str)
    done = Signal(str)              # 空串表示成功


class FirstRunDialog(QDialog):
    """带进度条的准备窗口。exec() 返回 True 表示环境已就绪。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拾光")
        self.setModal(True)
        self.setFixedSize(460, 190)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self.error = ""
        self._reporter = _Reporter()
        self._reporter.progress.connect(self._on_progress)
        self._reporter.done.connect(self._on_done)
        self._last_emit = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 24)
        outer.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(12)
        mark = QLabel("拾")
        mark.setObjectName("brandMark")
        mark.setFixedSize(38, 38)
        mark.setAlignment(Qt.AlignCenter)
        head.addWidget(mark)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel("首次运行，正在准备运行环境")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        sub = QLabel("解压内置的 Chromium 和 ffmpeg，只需要这一次")
        sub.setObjectName("hint")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        head.addLayout(title_col)
        head.addStretch(1)
        outer.addLayout(head)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet(
            "QProgressBar { background: %s; border: none; border-radius: 4px; }"
            "QProgressBar::chunk { background: %s; border-radius: 4px; }"
            % (theme.palette()["surface2"], theme.palette()["accent"]))
        outer.addWidget(self.bar)

        self.status = QLabel("准备中…")
        self.status.setObjectName("hint")
        outer.addWidget(self.status)
        outer.addStretch(1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not hasattr(self, "_started"):
            self._started = True
            threading.Thread(target=self._work, name="first-run", daemon=True).start()

    def _work(self) -> None:
        def report(done: int, total: int, name: str) -> None:
            # 上千个文件，不能每个都发信号；限到每秒 20 次
            now = time.time()
            if now - self._last_emit < 0.05 and done < total:
                return
            self._last_emit = now
            self._reporter.progress.emit(int(done / total * 1000), name)

        try:
            runtime.extract(report)
        except Exception as exc:            # noqa: BLE001 - 结果要显示给用户
            self._reporter.done.emit(str(exc))
            return
        self._reporter.done.emit("")

    def _on_progress(self, value: int, name: str) -> None:
        self.bar.setValue(value)
        short = name.rsplit("/", 1)[-1]
        self.status.setText("%.0f%%   %s" % (value / 10.0, short[:56]))

    def _on_done(self, error: str) -> None:
        self.error = error
        self.accept() if not error else self.reject()


def ensure_runtime(parent=None) -> Optional[str]:
    """需要的话弹窗解压。返回 None 表示可以继续，否则是错误信息。"""
    if runtime.is_ready():
        return None
    dialog = FirstRunDialog(parent)
    if dialog.exec() == QDialog.Accepted:
        return None
    return dialog.error or "运行环境准备被中断"
