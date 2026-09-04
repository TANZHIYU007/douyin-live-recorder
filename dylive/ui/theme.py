"""主题：浅色 / 深色两套调色板 + 全局 QSS。

Qt 的样式表没有变量，所以先定义调色板，再用格式化把颜色套进同一份模板。
两套主题共用模板，改版式只需要改一处。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

LIGHT = {
    "bg":        "#F4F5F7",     # 窗口底
    "surface":   "#FFFFFF",     # 卡片
    "surface2":  "#F1F3F6",     # 输入框、次级容器
    "surface3":  "#E7EAEF",     # hover
    "border":    "#E4E7EC",
    "border_hi": "#CBD2DC",

    "text":      "#1B1F27",
    "dim":       "#59616F",
    "muted":     "#8A93A2",

    "accent":    "#FE2C55",     # 抖音红，两套主题共用
    "accent_hi": "#FF4B6E",
    "accent_dk": "#FBC8D2",     # 主按钮禁用态
    "accent_ghost": "rgba(254, 44, 85, 0.10)",
    "on_accent": "#FFFFFF",
    "cyan":      "#0E9088",     # 在白底上要压暗才看得清
    "green":     "#12945A",
    "amber":     "#B4700B",
    "row_alt":   "#FAFBFC",
    "shadow":    "rgba(16, 24, 40, 0.06)",
}

DARK = {
    "bg":        "#0E1014",
    "surface":   "#161920",
    "surface2":  "#1D212A",
    "surface3":  "#232833",
    "border":    "#262B36",
    "border_hi": "#343B4A",

    "text":      "#E8EAF0",
    "dim":       "#9AA3B5",
    "muted":     "#667085",

    "accent":    "#FE2C55",
    "accent_hi": "#FF4B6E",
    "accent_dk": "#4A2330",
    "accent_ghost": "rgba(254, 44, 85, 0.16)",
    "on_accent": "#FFFFFF",
    "cyan":      "#25F4EE",
    "green":     "#34D399",
    "amber":     "#FBBF24",
    "row_alt":   "#12151B",
    "shadow":    "rgba(0, 0, 0, 0.35)",
}

PALETTES = {"light": LIGHT, "dark": DARK}
DEFAULT_MODE = "light"

FONT_STACK = ('"Microsoft YaHei UI", "Segoe UI", "PingFang SC", '
              '"Noto Sans CJK SC", sans-serif')   # 三个系统各挑一个，缺了往后退
MONO_STACK = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

KIND_LABELS = {
    "chat": "弹幕", "emoji": "表情", "gift": "礼物", "member": "进场",
    "social": "关注", "like": "点赞", "user_seq": "在线", "stats": "统计",
    "control": "状态", "fansclub": "粉丝团", "room_notice": "公告",
}


_current = DEFAULT_MODE


def set_mode(mode: str) -> None:
    """记住当前主题，控件里那些没法用 QSS 表达的颜色要按它取。"""
    global _current
    _current = mode if mode in PALETTES else DEFAULT_MODE


def current() -> str:
    return _current


def palette(mode: str = "") -> dict:
    return PALETTES.get(mode or _current, LIGHT)


def kind_colors(mode: str = "") -> dict:
    p = palette(mode)
    return {
        "chat":        p["text"],
        "emoji":       p["text"],
        "gift":        p["accent"],
        "social":      p["cyan"],
        "member":      p["muted"],
        "like":        p["muted"],
        "control":     p["amber"],
        "fansclub":    p["amber"],
        "stats":       p["muted"],
        "user_seq":    p["muted"],
        "room_notice": p["dim"],
    }


_QSS = """
* {
    font-family: %(font)s;
    font-size: 13px;
}
/* 只给顶层容器上底色。给 QWidget 设 background 会让卡片里的每个 label
   都画一块窗口底色的方块，看起来到处是补丁。 */
QWidget { color: %(text)s; }
QMainWindow, #root { background: %(bg)s; }
QLabel { background: transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
#settingsHost { background: transparent; }
QDialog { background: %(bg)s; }
QToolTip {
    background: %(surface)s;
    color: %(text)s;
    border: 1px solid %(border_hi)s;
    border-radius: 6px;
    padding: 5px 8px;
}

/* ---------- 顶栏 ---------- */
#topbar {
    background: %(surface)s;
    border-bottom: 1px solid %(border)s;
}
#brandMark {
    border-radius: 10px;
    color: #FFFFFF;
    font-size: 15px;
    font-weight: 800;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 %(accent)s, stop:1 #7B2FF7);
}
#brandName { font-size: 17px; font-weight: 700; letter-spacing: 0.5px; }
#brandSub  { color: %(muted)s; font-size: 11px; }

/* ---------- 输入 ---------- */
QLineEdit {
    background: %(surface2)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 9px 14px;
    selection-background-color: %(accent)s;
    selection-color: #FFFFFF;
}
QLineEdit:focus  { border: 1px solid %(accent)s; background: %(surface)s; }
QLineEdit:disabled { color: %(muted)s; background: %(surface3)s; }

QComboBox, QSpinBox {
    background: %(surface2)s;
    border: 1px solid %(border)s;
    border-radius: 9px;
    padding: 7px 10px;
    min-height: 18px;
}
QComboBox:hover, QSpinBox:hover { border-color: %(border_hi)s; }
QComboBox:focus, QSpinBox:focus { border-color: %(accent)s; }
QComboBox:disabled, QSpinBox:disabled { color: %(muted)s; background: %(surface3)s; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid %(dim)s;
    width: 0; height: 0; margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: %(surface)s;
    border: 1px solid %(border_hi)s;
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: %(accent)s;
    selection-color: #FFFFFF;
}
QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; }

/* ---------- 按钮 ---------- */
QPushButton {
    background: %(surface2)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 8px 16px;
    font-weight: 600;
}
QPushButton:hover   { background: %(surface3)s; border-color: %(border_hi)s; }
QPushButton:pressed { background: %(border)s; }
QPushButton:disabled { color: %(muted)s; background: %(surface3)s; }

QPushButton#primary {
    background: %(accent)s;
    border: none;
    color: %(on_accent)s;
    padding: 10px 24px;
    font-size: 14px;
}
QPushButton#primary:hover   { background: %(accent_hi)s; }
QPushButton#primary:pressed { background: #E01F46; }
QPushButton#primary:disabled { background: %(accent_dk)s; color: %(on_accent)s; }

QPushButton#stopBtn {
    background: %(accent_ghost)s;
    border: 1px solid %(accent)s;
    color: %(accent)s;
    padding: 10px 24px;
    font-size: 14px;
}
QPushButton#stopBtn:hover { background: rgba(254, 44, 85, 0.18); }

QPushButton#ghost {
    background: transparent;
    border: 1px solid %(border)s;
    padding: 7px 12px;
    font-weight: 500;
}
QPushButton#ghost:hover { background: %(surface2)s; }
QPushButton#iconBtn {
    background: transparent;
    border: 1px solid %(border)s;
    border-radius: 9px;
    padding: 6px 9px;
    font-size: 14px;
}
QPushButton#iconBtn:hover { background: %(surface2)s; }

/* ---------- 卡片 ---------- */
#card {
    background: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 14px;
}
#cardTitle {
    color: %(muted)s;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
}
#nickname   { font-size: 16px; font-weight: 700; }
#roomTitle  { color: %(dim)s; font-size: 12px; }
#hint       { color: %(muted)s; font-size: 11px; }
#sep        { background: %(border)s; }

#statTile {
    background: %(surface2)s;
    border: 1px solid %(border)s;
    border-radius: 12px;
}
#statValue { font-size: 19px; font-weight: 700; }
#statValueAccent { font-size: 19px; font-weight: 700; color: %(accent)s; }
#statValueCyan   { font-size: 19px; font-weight: 700; color: %(cyan)s; }
#fieldLabel { color: %(dim)s; }
#rowName { font-size: 13px; font-weight: 700; }
#rowMeta { color: %(muted)s; font-size: 11px; }
#statLabel { color: %(muted)s; font-size: 11px; }

#pillLive {
    background: rgba(18, 148, 90, 0.12);
    color: %(green)s;
    border: 1px solid rgba(18, 148, 90, 0.30);
    border-radius: 9px; padding: 2px 10px;
    font-size: 11px; font-weight: 700;
}
#pillOff {
    background: %(surface2)s;
    color: %(muted)s;
    border: 1px solid %(border)s;
    border-radius: 9px; padding: 2px 10px;
    font-size: 11px; font-weight: 700;
}
#pillRec {
    background: %(accent_ghost)s;
    color: %(accent)s;
    border: 1px solid rgba(254, 44, 85, 0.35);
    border-radius: 9px; padding: 2px 10px;
    font-size: 11px; font-weight: 700;
}
#pillWait {
    background: rgba(180, 112, 11, 0.12);
    color: %(amber)s;
    border: 1px solid rgba(180, 112, 11, 0.28);
    border-radius: 9px; padding: 2px 10px;
    font-size: 11px; font-weight: 700;
}
/* 弹幕这一路出问题时的横幅。用琥珀色而不是红色：画面还在正常录，
   这是「有一半没成」而不是「整个挂了」。 */
#warnBar {
    background: rgba(180, 112, 11, 0.10);
    color: %(amber)s;
    border: 1px solid rgba(180, 112, 11, 0.30);
    border-radius: 8px; padding: 8px 12px;
    font-size: 12px; font-weight: 600;
}

/* ---------- 房间列表 ---------- */
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    background: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 12px;
    margin: 0 0 8px 0;
}
QListWidget::item:hover    { border-color: %(border_hi)s; }
QListWidget::item:selected { border: 1px solid %(accent)s; background: %(surface)s; }

/* 文件列表是紧凑的纯文本行，不要房间列表那种大卡片外框 */
#filesList { background: transparent; }
#filesList::item {
    background: transparent;
    border: none;
    border-radius: 0;
    margin: 0;
    padding: 3px 2px;
    color: %(dim)s;
}
#filesList::item:hover, #filesList::item:selected {
    background: transparent; border: none; color: %(text)s;
}

#preview {
    background: #101215;
    border: 1px solid %(border)s;
    border-radius: 12px;
    color: %(muted)s;
}

/* ---------- 勾选 ---------- */
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 5px;
    border: 1px solid %(border_hi)s;
    background: %(surface)s;
}
QCheckBox::indicator:hover   { border-color: %(dim)s; }
QCheckBox::indicator:checked {
    background: %(accent)s; border-color: %(accent)s; image: url(%(check)s);
}
QCheckBox:disabled { color: %(muted)s; }

/* ---------- 标签页 ---------- */
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab {
    background: transparent;
    color: %(muted)s;
    padding: 7px 16px;
    margin-right: 4px;
    border-radius: 9px;
    font-weight: 600;
}
QTabBar::tab:hover    { color: %(dim)s; }
QTabBar::tab:selected { background: %(surface2)s; color: %(text)s; }

/* ---------- 表格 ---------- */
QTableView {
    background: %(surface)s;
    border: none;
    gridline-color: transparent;
    selection-background-color: %(surface3)s;
    selection-color: %(text)s;
    alternate-background-color: %(row_alt)s;
    outline: none;
}
QTableView::item { padding: 4px 8px; border: none; }
QHeaderView::section {
    background: %(surface)s;
    color: %(muted)s;
    border: none;
    border-bottom: 1px solid %(border)s;
    padding: 8px;
    font-size: 11px;
    font-weight: 700;
}

QPlainTextEdit {
    background: %(surface)s;
    border: none;
    color: %(dim)s;
    font-family: %(mono)s;
    font-size: 12px;
    padding: 6px;
}

QProgressBar {
    background: %(surface2)s; border: none; border-radius: 4px;
}
QProgressBar::chunk { background: %(accent)s; border-radius: 4px; }

/* ---------- 滚动条 ---------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; }
QScrollBar::handle:vertical {
    background: %(border_hi)s; border-radius: 5px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: %(muted)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px 2px 2px; }
QScrollBar::handle:horizontal {
    background: %(border_hi)s; border-radius: 5px; min-width: 32px;
}
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* 分隔条只在这里定颜色。宽度必须用 setHandleWidth() 设 —— QSS 的 width
   对 QSplitter::handle 不生效，写在这里会误以为留了间距，实际是 0。 */
QSplitter::handle { background: transparent; }
"""


def _checkmark_icon(mode: str) -> str:
    """QSS 画不出对勾，现画一张 16px 的白色勾图给勾选框用。

    必须在 QApplication 建好之后调用（QPixmap 需要 GUI 环境）。
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainter, QPen, QPixmap

    path = Path(tempfile.gettempdir()) / ("lumina_check_%s.png" % mode)
    pix = QPixmap(16, 16)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(Qt.white, 2.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline([QPointF(3.5, 8.2), QPointF(6.6, 11.3), QPointF(12.5, 4.8)])
    painter.end()
    pix.save(str(path))
    return path.as_posix()


def stylesheet(mode: str = "") -> str:
    mode = mode or _current
    try:
        check = _checkmark_icon(mode)
    except Exception:               # noqa: BLE001 - 没图也就是少个勾，不该拦住启动
        check = ""
    values = dict(palette(mode), font=FONT_STACK, mono=MONO_STACK, check=check)
    return _QSS % values
