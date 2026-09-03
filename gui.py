#!/usr/bin/env python
"""拾光 Lumina —— 图形界面入口。

    python gui.py

打包成 exe 后是 --windowed 模式，没有控制台。启动阶段真出了异常的话，
不做处理就是双击没反应 —— 所以这里兜一层，把 traceback 写进日志文件
并弹个框告诉用户去哪看。
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path


def _crash_log() -> Path:
    from dylive.paths import app_data_dir    # 只依赖标准库，早期导入也安全
    return app_data_dir() / "crash.log"


def _on_uncaught(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    path = _crash_log()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n===== %s =====\n%s" % (datetime.now().isoformat(" ", "seconds"), text))
    except OSError:
        path = None

    sys.stderr.write(text) if sys.stderr else None
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(
            None, "拾光 出错了",
            "%s\n\n详细信息已写入：\n%s" % (str(exc) or exc_type.__name__, path or "（写入失败）"))
    except Exception:       # noqa: BLE001 - 连报错都失败就只能算了
        pass


def _run_selftest() -> int:
    """--selftest：不开主界面，只跑一遍环境检查并把结果显示出来。"""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from dylive.selftest import run as check
    from dylive.ui.first_run import ensure_runtime
    from dylive.ui.theme import stylesheet

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(stylesheet())
    # 先把内置运行时准备好，否则自检测的是「还没解压」这个废话
    error = ensure_runtime()
    if error:
        QMessageBox.critical(None, "拾光 环境自检", "运行环境准备失败：\n%s" % error)
        return 1
    ok, text = check()

    box = QMessageBox()
    box.setWindowTitle("拾光 环境自检")
    box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
    box.setText("全部通过，可以正常录制" if ok else "检查出问题了")
    box.setDetailedText(text)
    box.exec()
    return 0 if ok else 1


def main() -> int:
    sys.excepthook = _on_uncaught
    if "--selftest" in sys.argv:
        return _run_selftest()
    from dylive.ui.window import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
