"""界面里复用的小部件。

配色尽量交给 QSS（objectName + 全局样式表），换主题时只要重新 setStyleSheet
就整体生效。只有 QSS 表达不了的地方（弹幕按类型上色）才去 theme.palette() 取。
"""

from __future__ import annotations

from collections import deque
from typing import Optional, Sequence

from PySide6.QtCore import (QAbstractTableModel, QModelIndex,
                            QSortFilterProxyModel, QSize, Qt)
from PySide6.QtGui import (QBrush, QColor, QImage, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import theme
from .theme import KIND_LABELS

STATE_PILL = {
    "recording": ("● 录制中", "pillRec"),
    "processing": ("处理中", "pillWait"),
    "waiting":   ("守候中", "pillWait"),
    "starting":  ("启动中", "pillWait"),
    "error":     ("出错", "pillOff"),
    "idle":      ("未开始", "pillOff"),
}


def rounded_pixmap(data: bytes, w: int, h: int, radius: int) -> Optional[QPixmap]:
    """把图片裁成圆角矩形，居中裁剪不变形。"""
    src = QPixmap()
    if not data or not src.loadFromData(data):
        return None
    scaled = src.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    cropped = scaled.copy(max(0, (scaled.width() - w) // 2),
                          max(0, (scaled.height() - h) // 2), w, h)

    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return out


def circular_pixmap(data: bytes, size: int) -> Optional[QPixmap]:
    return rounded_pixmap(data, size, size, size // 2)


def set_pill(label: QLabel, state: str) -> None:
    """按状态换 objectName，让 QSS 重新上色。"""
    text, name = STATE_PILL.get(state, STATE_PILL["idle"])
    label.setText(text)
    label.setObjectName(name)
    label.style().unpolish(label)
    label.style().polish(label)


class Card(QFrame):
    """带标题的圆角卡片，内容塞进 self.body。"""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)

        if title:
            label = QLabel(title.upper())
            label.setObjectName("cardTitle")
            outer.addWidget(label)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addLayout(self.body)


class StatTile(QFrame):
    """一个指标：大号数值 + 说明文字。"""

    def __init__(self, label: str, value: str = "—", accent: str = ""):
        super().__init__()
        self.setObjectName("statTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)

        self.value = QLabel(value)
        self.value.setObjectName({"accent": "statValueAccent",
                                  "cyan": "statValueCyan"}.get(accent, "statValue"))
        caption = QLabel(label)
        caption.setObjectName("statLabel")
        lay.addWidget(self.value)
        lay.addWidget(caption)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class PreviewView(QWidget):
    """实时画面。

    自己画，不用 QLabel + setPixmap：早先的版本为了让竖屏画面不留黑边，会在
    resizeEvent 里反过来改自己的 maximumWidth，结果和布局系统互相触发，宽度要
    好几轮才收敛，缩放窗口时肉眼可见地抖。自绘就没有这个反馈环 —— 控件老实
    占满分给它的位置，画面按比例居中缩放，尺寸永远一次到位。

    左右两侧的留白用深色铺满，读起来就是个播放器画框，不像"没对齐"。
    """

    BACKDROP = "#101215"
    DEFAULT_ASPECT = 9 / 16         # 抖音默认竖屏

    def __init__(self, hint: str = "选中正在录制的房间即可预览"):
        super().__init__()
        self.setObjectName("preview")
        self.setMinimumSize(140, 100)
        # 高度跟着宽度按画面比例走。用 Qt 原生的 heightForWidth，而不是自己在
        # resizeEvent 里改尺寸 —— 后者会和布局互相触发，之前就因为这个抖过。
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._hint = hint
        self._frame: Optional[QPixmap] = None
        self._scaled: Optional[QPixmap] = None   # 按当前控件尺寸缩好的那一份
        self._aspect = self.DEFAULT_ASPECT

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(100, int(width / self._aspect))

    def show_image(self, img: QImage) -> None:
        """收下一帧已经解好码的画面。解码在别的线程做完了，这里只贴图。"""
        if img.isNull() or img.height() <= 0:
            return
        self._set_frame(QPixmap.fromImage(img))

    def show_frame(self, data: bytes) -> None:
        """从原始字节收一帧。只有测试和静态图会走这条路。"""
        pix = QPixmap()
        if not pix.loadFromData(data) or pix.height() <= 0:
            return
        self._set_frame(pix)

    def _set_frame(self, pix: QPixmap) -> None:
        self._frame = pix
        self._scaled = None
        aspect = pix.width() / pix.height()
        if abs(aspect - self._aspect) > 0.01:
            self._aspect = aspect       # 横屏/竖屏切换，让布局重算高度
            self.updateGeometry()
        self.update()

    def clear_frame(self, hint: str = "") -> None:
        hint = hint or self._hint
        if self._frame is None and hint == self._hint:
            return          # 已经是这个样子了，别每 120ms 白重绘一次
        self._frame = None
        self._scaled = None
        self._hint = hint
        if self._aspect != self.DEFAULT_ASPECT:
            self._aspect = self.DEFAULT_ASPECT
            self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scaled = None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = self.rect()
        path = QPainterPath()
        path.addRoundedRect(rect.x(), rect.y(), rect.width(), rect.height(), 12, 12)
        painter.setClipPath(path)
        painter.fillPath(path, QColor(self.BACKDROP))

        if self._frame is None:
            painter.setPen(QColor(theme.palette()["muted"]))
            painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, self._hint)
            return

        # 缩放结果缓存起来：paintEvent 不只在新帧到达时触发（遮挡、切主题、
        # 滚动都会触发），每次都重缩一遍纯属白干
        if self._scaled is None:
            self._scaled = self._frame.scaled(rect.size(), Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation)
        scaled = self._scaled
        painter.drawPixmap((rect.width() - scaled.width()) // 2,
                           (rect.height() - scaled.height()) // 2, scaled)


class RoomRow(QWidget):
    """监测列表里的一行：头像 + 昵称 + 状态 + 时长/大小/弹幕数。"""

    AVATAR = 34

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        self.avatar = QLabel()
        self.avatar.setFixedSize(self.AVATAR, self.AVATAR)
        self._reset_avatar()
        lay.addWidget(self.avatar, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = QLabel("—")
        self.name.setObjectName("rowName")
        self.pill = QLabel()
        self.pill.setObjectName("pillOff")
        top.addWidget(self.name, 1)
        top.addWidget(self.pill, 0, Qt.AlignRight)
        self.meta = QLabel("")
        self.meta.setObjectName("rowMeta")
        col.addLayout(top)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)

    def _reset_avatar(self) -> None:
        self.avatar.setStyleSheet(
            "background: %s; border-radius: %dpx;"
            % (theme.palette()["surface2"], self.AVATAR // 2))

    def set_avatar(self, data: bytes) -> None:
        pix = circular_pixmap(data, self.AVATAR)
        if pix:
            self.avatar.setPixmap(pix)

    def update_row(self, name: str, state: str, meta: str) -> None:
        self.name.setText(name)
        self.name.setToolTip(name)          # 窄栏里长昵称会被截，鼠标悬停能看全
        self.meta.setText(meta)
        set_pill(self.pill, state)

    def sizeHint(self) -> QSize:
        return QSize(240, 64)


class RoomHeader(QWidget):
    """详情区顶部：头像 + 昵称 + 状态 + 标题 + 房间号。"""

    AVATAR = 42

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(12)

        self.avatar = QLabel()
        self.avatar.setFixedSize(self.AVATAR, self.AVATAR)
        self._reset_avatar()
        lay.addWidget(self.avatar, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.nickname = QLabel("未选择房间")
        self.nickname.setObjectName("nickname")
        self.pill = QLabel()
        self.pill.setObjectName("pillOff")
        top.addWidget(self.nickname)
        top.addWidget(self.pill)
        top.addStretch(1)
        self.title = QLabel("")
        self.title.setObjectName("roomTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("hint")
        col.addLayout(top)
        col.addWidget(self.title)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)

    def _reset_avatar(self) -> None:
        self.avatar.setStyleSheet(
            "background: %s; border-radius: %dpx;"
            % (theme.palette()["surface2"], self.AVATAR // 2))

    def set_avatar(self, data: bytes) -> None:
        pix = circular_pixmap(data, self.AVATAR)
        if pix:
            self.avatar.setPixmap(pix)

    def clear_avatar(self) -> None:
        self.avatar.setPixmap(QPixmap())
        self._reset_avatar()

    def show_room(self, info, state: str) -> None:
        self.nickname.setText(info.nickname or info.web_rid)
        self.title.setText(info.title or "（无标题）")
        bits = ["房间号 " + info.web_rid]
        if info.online:
            bits.append(info.online + "人在线")
        self.meta.setText("  ·  ".join(bits))
        set_pill(self.pill, state)

    def show_empty(self) -> None:
        self.nickname.setText("未选择房间")
        self.title.setText("")
        self.meta.setText("")
        self.clear_avatar()
        set_pill(self.pill, "idle")


class DanmakuModel(QAbstractTableModel):
    """弹幕表格。用 deque 存最近若干条，超出上限从头裁。"""

    HEADERS = ("时间", "类型", "用户", "内容")

    def __init__(self, limit: int = 3000):
        super().__init__()
        self._rows: deque = deque()
        self._limit = limit
        self._colors = theme.kind_colors()

    # -- Qt 接口 ----------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        ev = self._rows[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == 0:
                secs = max(0, int(ev.offset))
                return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
            if col == 1:
                return KIND_LABELS.get(ev.kind, ev.kind)
            if col == 2:
                return ev.user_name or "—"
            # 进场/关注这类消息的正文本来就是「昵称 来了」，昵称已经在上一列了
            text = ev.content
            if (ev.kind in ("member", "social", "like", "gift", "fansclub")
                    and ev.user_name and text.startswith(ev.user_name)):
                text = text[len(ev.user_name):].lstrip(" ：:") or text
            return text
        if role == Qt.ForegroundRole:
            key = ev.kind if col == 3 else "member"
            return QBrush(QColor(self._colors.get(key, self._colors["room_notice"])))
        if role == Qt.ToolTipRole and col == 3:
            return ev.content
        return None

    # -- 自用 -------------------------------------------------------------

    def extend(self, events: Sequence) -> None:
        if not events:
            return
        events = list(events)[-self._limit:]

        overflow = len(self._rows) + len(events) - self._limit
        if overflow > 0:
            drop = min(overflow, len(self._rows))
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            for _ in range(drop):
                self._rows.popleft()
            self.endRemoveRows()

        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(events) - 1)
        self._rows.extend(events)
        self.endInsertRows()

    def replace(self, events: Sequence) -> None:
        self.beginResetModel()
        self._rows = deque(list(events)[-self._limit:])
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()

    def restyle(self) -> None:
        self._colors = theme.kind_colors()
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, len(self.HEADERS) - 1),
                                  [Qt.ForegroundRole])

    def event_at(self, row: int):
        return self._rows[row] if 0 <= row < len(self._rows) else None


class KindFilter(QSortFilterProxyModel):
    """按事件类型过滤弹幕列表。kinds 为 None 表示不过滤。"""

    def __init__(self):
        super().__init__()
        self.kinds: Optional[set] = None

    def set_kinds(self, kinds: Optional[Sequence[str]]) -> None:
        self.kinds = set(kinds) if kinds is not None else None
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        if self.kinds is None:
            return True
        ev = self.sourceModel().event_at(row)
        return ev is not None and ev.kind in self.kinds


class Field(QWidget):
    """一行设置：左标签 + 右控件。"""

    def __init__(self, label: str, widget: QWidget, stretch: int = 0, width: int = 68):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        text = QLabel(label)
        text.setObjectName("fieldLabel")
        text.setFixedWidth(width)
        lay.addWidget(text)
        lay.addWidget(widget, 1 if stretch else 0)
        if not stretch:
            lay.addStretch(1)


def separator() -> QFrame:
    line = QFrame()
    line.setObjectName("sep")
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return line
