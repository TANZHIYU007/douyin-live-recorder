"""系统托盘图标与开播通知。

守候模式下这个程序一挂就是几小时，界面开着纯属占地方。托盘解决两件事：

* **收起来还在跑。** 关窗口不等于停止录制 —— 但这件事必须让用户确切知道，
  否则就成了「我明明关了它怎么还在录」。所以第一次收起时会弹一条说明。
* **开播了得有人告诉你。** 守候的意义就在于人不用盯着，那么开播和收工这两个
  时刻就得主动推过来。

图标跟着状态走：有房间在录是抖音红，全都在守候是灰的 —— 扫一眼托盘就知道
现在是什么情况，不用把窗口翻出来。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .icons import status_icon

log = logging.getLogger("tray")

NOTIFY_MS = 6000


def make_icon(active: bool) -> QIcon:
    """品牌图标右下角加状态点：红色在录，灰色待命。"""
    return status_icon(active)


class Tray:
    """托盘的一层薄包装。系统不支持托盘时整个对象退化成空操作。"""

    def __init__(self, parent, on_show: Callable[[], None],
                 on_start_all: Callable[[], None],
                 on_stop_all: Callable[[], None],
                 on_quit: Callable[[], None],
                 enabled: bool = True):
        # 用户关掉了，或者这个桌面环境压根没有托盘 —— 两种情况下所有方法
        # 都退化成空操作，调用方不用到处写 if
        self.available = enabled and QSystemTrayIcon.isSystemTrayAvailable()
        self._icon: Optional[QSystemTrayIcon] = None
        self._active = False
        self._explained = False         # 「收进托盘了」只说一次
        if not self.available:
            if enabled:
                log.info("这个桌面环境没有系统托盘，相关功能关闭")
            return

        self._icon = QSystemTrayIcon(make_icon(False), parent)
        self._icon.setToolTip("拾光 · 抖音直播录制")

        menu = QMenu(parent)
        for text, slot in (("显示主窗口", on_show),
                           ("全部开始", on_start_all),
                           ("全部停止", on_stop_all)):
            action = QAction(text, parent)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("退出拾光", parent)
        quit_action.triggered.connect(on_quit)
        menu.addAction(quit_action)

        self._icon.setContextMenu(menu)
        # 双击（macOS 上是单击）把窗口叫回来
        self._icon.activated.connect(
            lambda reason: on_show()
            if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger)
            else None)
        self._icon.show()

    # -- 状态 -------------------------------------------------------------

    def set_active(self, active: bool, summary: str = "") -> None:
        """有没有房间在录。变了才重画图标，省得每秒重绘一次。"""
        if self._icon is None:
            return
        if active != self._active:
            self._active = active
            self._icon.setIcon(make_icon(active))
        self._icon.setToolTip("拾光 · " + (summary or "待命中"))

    # -- 通知 -------------------------------------------------------------

    def notify(self, title: str, message: str) -> None:
        if self._icon is None:
            return
        self._icon.showMessage(title, message, make_icon(self._active), NOTIFY_MS)

    def explain_hidden_once(self) -> None:
        """第一次收进托盘时说清楚：窗口没了，录制还在。"""
        if self._icon is None or self._explained:
            return
        self._explained = True
        self.notify("拾光还在后台运行",
                    "窗口已收进托盘，录制没有中断。要真正退出请用托盘菜单里的"
                    "「退出拾光」。")

    def hide(self) -> None:
        if self._icon is not None:
            self._icon.hide()
