"""把 ffmpeg 吐出的 JPEG 变成界面能直接贴的 QImage。

    ffmpeg ──管道──▶ 读取线程（只切帧，不解码）
                        └──▶ 派发线程（解码 + 预缩放）
                               └──信号──▶ 界面线程（只 drawPixmap）

解码原来是在界面线程的定时器里做的，问题有两个：定时器 120ms 跳一次，
和直播源的帧间隔对不上，画面节奏是忽快忽慢的；而且它每次都把**同一张**
图重解一遍 —— 实测 16ms 轮询下 6 秒白解码 273 次、白烧 119ms/s 的 CPU。

改成按帧推送之后，一帧只解一次，什么时候有新帧什么时候画，节奏和直播源
一致；界面线程剩下的活只有一次贴图。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from ..preview import FOLLOW_SOURCE, PreviewStream

log = logging.getLogger("preview")


class PreviewFeed(QObject):
    """PreviewStream 的 Qt 外壳：起停、解码、把帧发到界面线程。"""

    frame = Signal(QImage)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._stream: Optional[PreviewStream] = None
        self._target = 0        # 目标像素宽；界面线程写，派发线程读
        self.rid = ""

    # -- 起停 -------------------------------------------------------------

    def start(self, ffmpeg: str, url: str, rid: str = "",
              width: int = 640, fps: int = FOLLOW_SOURCE) -> None:
        self.stop()
        self.rid = rid
        self._stream = PreviewStream(ffmpeg, url, width=width, fps=fps,
                                     on_frame=self._decode)
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        self.rid = ""
        if stream is not None:
            stream.stop()

    @property
    def alive(self) -> bool:
        return self._stream is not None and self._stream.alive

    @property
    def stats(self) -> str:
        s = self._stream
        if s is None:
            return ""
        return "%d 帧" % s.frames + ("（跳 %d）" % s.dropped if s.dropped else "")

    def set_target_width(self, px: int) -> None:
        """告诉派发线程画面最终要显示多宽，缩放就在那边顺手做掉。"""
        self._target = max(0, int(px))

    # -- 派发线程 ---------------------------------------------------------

    def _decode(self, jpeg: bytes, seq: int) -> None:
        img = QImage.fromData(jpeg, "JPG")
        if img.isNull():
            return
        want = self._target
        # 只往小缩。放大交给界面那一步做，在这里放大等于把大图搬过线程边界
        if 0 < want < img.width():
            img = img.scaledToWidth(want, Qt.SmoothTransformation)
        # 跨线程发信号：Qt 自动排队到界面线程，QImage 是隐式共享的，安全
        self.frame.emit(img)
