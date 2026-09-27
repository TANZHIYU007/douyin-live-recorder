"""主窗口的人机交互冒烟测试。"""

from dylive.ui import smoke
from dylive.ui.settings import AppSettings
from dylive.ui.window import MainWindow, fit_window_dimensions


def test_窗口尺寸适配常见与极小工作区():
    assert fit_window_dimensions(1920, 1040) == (1320, 860, 1080, 700)
    assert fit_window_dimensions(1366, 728) == (1320, 688, 1080, 688)
    assert fit_window_dimensions(1024, 728) == (984, 688, 984, 688)
    assert fit_window_dimensions(800, 560) == (760, 520, 760, 520)
    assert fit_window_dimensions(600, 400) == (600, 400, 600, 400)


def test_主窗口真实交互流程(qapp, monkeypatch, tmp_path):
    settings = AppSettings(out_dir=str(tmp_path), tray=False, rooms=[])
    monkeypatch.setattr("dylive.ui.window.AppSettings.load", lambda: settings)
    monkeypatch.setattr(settings, "save", lambda: None)

    window = MainWindow()
    window.show()
    try:
        steps = smoke.exercise(window)
    finally:
        window._quitting = True
        window.close()

    assert "通过：主窗口已显示" in steps
    assert "通过：设置对话框可取消关闭" in steps
    assert "通过：空列表开始/停止安全" in steps
