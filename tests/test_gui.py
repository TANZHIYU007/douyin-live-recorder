"""图形入口里不依赖窗口的发布自检路径。"""

import gui
from dylive import runtime, selftest


def test_headless_自检准备运行时并返回成功(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "extract", lambda: calls.append("extract"))
    monkeypatch.setattr(runtime, "apply_env", lambda: calls.append("env"))
    monkeypatch.setattr(selftest, "run", lambda: (True, "全部通过"))

    assert gui._run_selftest_headless() == 0
    assert calls == ["extract", "env"]


def test_headless_自检失败时返回非零(monkeypatch):
    monkeypatch.setattr(runtime, "extract", lambda: None)
    monkeypatch.setattr(runtime, "apply_env", lambda: None)
    monkeypatch.setattr(selftest, "run", lambda: (False, "存在问题"))

    assert gui._run_selftest_headless() == 1
