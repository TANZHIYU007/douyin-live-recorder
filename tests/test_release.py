"""发布版本一致性与离线成品自检：不让网络状态掩盖本地打包错误。"""

import re
from pathlib import Path

import pytest

import build_exe
import gui
from dylive import __version__, runtime, selftest


def test_release_versions_and_notes_agree(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"$', project, re.M).group(1) == __version__
    assert build_exe.APP_VERSION == __version__
    notes = root / "docs" / "releases" / (__version__ + ".md")
    assert notes.is_file() and "v" + __version__ in notes.read_text(encoding="utf-8")
    monkeypatch.setattr(build_exe, "BUILD", tmp_path)
    version = build_exe.make_version_file().read_text(encoding="utf-8")
    assert "StringStruct('ProductVersion', '%s')" % __version__ in version


@pytest.mark.parametrize("include_network", [False, True])
def test_offline_selftest_skips_only_network(tmp_path, monkeypatch, include_network):
    called = []
    monkeypatch.setattr(selftest, "_report_path", lambda: tmp_path / "selftest.txt")
    monkeypatch.setattr(selftest, "CHECKS", [
        ("运行时", lambda: (called.append("runtime") is None, "ok")),
        ("网络", lambda: (called.append("network") is not None, "unreachable")),
    ])
    ok, report = selftest.run(include_network=include_network)
    assert ok is not include_network
    assert called == (["runtime", "network"] if include_network else ["runtime"])
    if not include_network:
        assert "跳过" in report and "不验证抖音连接" in report


def test_headless_offline_still_checks_runtime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "extract", lambda: calls.append("extract"))
    monkeypatch.setattr(runtime, "apply_env", lambda: calls.append("env"))
    monkeypatch.setattr(selftest, "run", lambda **kwargs: (calls.append(kwargs) is None, "ok"))
    assert gui._run_selftest_headless(offline=True) == 0
    assert calls == ["extract", "env", {"include_network": False}]
