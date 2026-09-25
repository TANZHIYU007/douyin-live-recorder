"""让 tests/ 能直接 import 仓库根目录下的 dylive 包，并提供共用的 Qt fixture。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def qapp():
    """整个测试会话共用一个 QApplication —— Qt 不允许建第二个。

    没有显示器时用 offscreen 平台插件，CI 上也能跑。
    """
    pytest.importorskip("PySide6", reason="没装 PySide6，跳过界面相关用例")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
