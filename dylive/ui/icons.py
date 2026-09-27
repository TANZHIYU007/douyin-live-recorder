"""应用图标：窗口、任务栏、托盘和安装包共用同一份视觉资产。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QRegion


ICON_SOURCE = Path(__file__).with_name("assets") / "lumina-icon-v2.png"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def app_pixmap(size: int) -> QPixmap:
    """把生成图裁掉透明空边，再留少量安全区缩到指定尺寸。"""
    source = QPixmap(str(ICON_SOURCE))
    if source.isNull():
        return QPixmap()
    bounds = QRegion(source.mask()).boundingRect()
    if bounds.isValid():
        source = source.copy(bounds)

    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    pad = max(1, round(size * 0.035))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.drawPixmap(pad, pad, size - pad * 2, size - pad * 2, source)
    painter.end()
    return canvas


def app_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(app_pixmap(size))
    return icon


def status_icon(active: bool) -> QIcon:
    """托盘沿用品牌图标，右下角状态点区分录制中与待命。"""
    icon = QIcon()
    for size in ICON_SIZES[:6]:
        pix = app_pixmap(size)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        diameter = max(5, round(size * 0.30))
        margin = max(1, round(size * 0.03))
        rect = QRectF(size - diameter - margin, size - diameter - margin,
                      diameter, diameter)
        painter.setPen(QPen(QColor("#FFFFFF"), max(1, round(size * 0.07))))
        painter.setBrush(QColor("#FF3158") if active else QColor("#8A93A0"))
        painter.drawEllipse(rect)
        painter.end()
        icon.addPixmap(pix)
    return icon
