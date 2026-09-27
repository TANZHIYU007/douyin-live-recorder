"""打包成品的真实 Qt 交互冒烟测试。

这不是普通的 import 检查：它会通过 Qt 的测试输入接口发送鼠标和键盘事件，
实际操作主窗口与设置对话框。发布构建使用隔离配置目录运行，不会碰用户设置。
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea, QTabWidget

REPORT_ENV = "LUMINA_GUI_SMOKE_REPORT"


def _check(condition: bool, label: str, steps: list[str]) -> None:
    if not condition:
        raise AssertionError(label)
    steps.append("通过：" + label)


def exercise(window) -> list[str]:
    """像用户一样操作一次主窗口；失败时抛出带步骤的异常。"""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication 尚未创建")
    steps: list[str] = []
    app.processEvents()

    _check(window.isVisible(), "主窗口已显示", steps)
    screen = window.screen() or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        _check(window.width() <= available.width(), "窗口宽度没有超出屏幕", steps)
        _check(window.height() <= available.height(), "窗口高度没有超出屏幕", steps)

    # 输入框：真正发送键盘事件，但不点添加，避免访问任意直播间。
    window.input.clear()
    window.input.setFocus()
    QTest.keyClicks(window.input, "123456789")
    _check(window.input.text() == "123456789", "直播间输入框可键入", steps)
    window.input.clear()

    original_filter = window.view_filter.currentIndex()
    window.view_filter.setFocus()
    QTest.keyClick(window.view_filter, Qt.Key_End)
    _check(window.view_filter.currentIndex() == window.view_filter.count() - 1,
           "弹幕筛选可用键盘切换", steps)
    window.view_filter.setCurrentIndex(original_filter)

    tabs = window.findChild(QTabWidget)
    _check(tabs is not None and tabs.count() >= 2, "弹幕与运行日志标签存在", steps)
    for index in range(tabs.count()):
        QTest.mouseClick(tabs.tabBar(), Qt.LeftButton,
                         pos=tabs.tabBar().tabRect(index).center())
        app.processEvents()
        _check(tabs.currentIndex() == index, "标签页 %d 可点击" % (index + 1), steps)

    dialog_result: dict[str, object] = {}

    def operate_dialog() -> None:
        dialog = app.activeModalWidget()
        try:
            _check(dialog is not None, "设置对话框已打开", steps)
            _check(dialog.windowTitle() == "录制设置", "设置对话框标题正确", steps)
            _check(dialog.height() <= dialog.screen().availableGeometry().height(),
                   "设置对话框没有超出屏幕", steps)

            scroll = dialog.findChild(QScrollArea)
            _check(scroll is not None, "设置内容可以滚动", steps)

            original_theme = dialog.theme_mode.currentData()
            dialog.theme_mode.setCurrentIndex(1 - dialog.theme_mode.currentIndex())
            _check(dialog.theme_mode.currentData() != original_theme,
                   "主题可以在设置中切换", steps)
            dialog.theme_mode.setCurrentIndex(0 if original_theme == "light" else 1)

            # 较矮屏幕上下面的选项不在可视区，先像用户一样滚到相应控件，
            # 再用键盘切换。直接向屏幕外的控件发送鼠标事件会造成误判。
            scroll.ensureWidgetVisible(dialog.cb_embed, 20, 20)
            app.processEvents()
            was_embed = dialog.cb_embed.isChecked()
            dialog.cb_embed.setFocus()
            QTest.keyClick(dialog.cb_embed, Qt.Key_Space)
            app.processEvents()
            _check(dialog.cb_sub_replace.isEnabled() != was_embed,
                   "字幕总开关会联动子选项", steps)
            QTest.keyClick(dialog.cb_embed, Qt.Key_Space)

            if dialog.cb_tray.isChecked():
                scroll.ensureWidgetVisible(dialog.cb_tray, 20, 20)
                app.processEvents()
                dialog.cb_tray.setFocus()
                QTest.keyClick(dialog.cb_tray, Qt.Key_Space)
                app.processEvents()
                _check(not dialog.cb_tray.isChecked(), "托盘开关可操作", steps)
                _check(not dialog.cb_minimize.isEnabled()
                       and not dialog.cb_notify.isEnabled(),
                       "关闭托盘会禁用托盘相关选项", steps)
            dialog_result["handled"] = True
        except Exception as exc:  # noqa: BLE001 - 必须带回外层后让进程失败
            dialog_result["error"] = exc
        finally:
            if isinstance(dialog, QDialog):
                dialog.reject()

    QTimer.singleShot(100, operate_dialog)
    QTest.mouseClick(window.settings_btn, Qt.LeftButton)
    if "error" in dialog_result:
        raise dialog_result["error"]
    _check(bool(dialog_result.get("handled")), "设置对话框可取消关闭", steps)

    # 空列表下点开始/停止不应崩溃；有用户房间时不触碰，避免意外开录。
    if not window.rows:
        QTest.mouseClick(window.start_all_btn, Qt.LeftButton)
        QTest.mouseClick(window.stop_all_btn, Qt.LeftButton)
        app.processEvents()
        _check(window.manager.active_count == 0, "空列表开始/停止安全", steps)

    _check(window.more_btn.menu() is not None
           and len(window.more_btn.menu().actions()) >= 3,
           "低频操作已收进更多菜单", steps)

    return steps


def _write_report(text: str) -> None:
    target = os.environ.get(REPORT_ENV, "")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def schedule(window) -> None:
    """主窗口进入事件循环后执行，并用进程退出码报告结果。"""
    app = QApplication.instance()

    def run() -> None:
        try:
            steps = exercise(window)
            _write_report("\n".join(steps) + "\n")
            code = 0
        except Exception:  # noqa: BLE001 - 成品测试需要完整失败报告
            _write_report(traceback.format_exc())
            code = 2
        window._quitting = True
        window.close()
        app.exit(code)

    QTimer.singleShot(500, run)
