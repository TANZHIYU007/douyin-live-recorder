"""录后文件变化能自动同步到录像库和播放入口。"""

import time
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest

from dylive.processing import PostProcessQueue
from dylive.ui import library
from dylive.ui.settings import AppSettings


def _wait(qapp, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


@pytest.mark.parametrize("fails", [False, True])
def test_finished_task_refreshes_files_size_selection_and_playback(qapp, tmp_path,
                                                                 monkeypatch, fails):
    folder = tmp_path / "主播"
    folder.mkdir()
    prefix = folder / "20261019_200000_直播"
    source = prefix.with_suffix(".mp4")
    source.write_bytes(b"source")
    jsonl = prefix.with_suffix(".jsonl")
    jsonl.write_text("{}", encoding="utf-8")
    # 新场排在第一行，处理较旧的第二行，刷新后仍应选中第二行。
    (folder / "20261020_200000_直播.mp4").write_bytes(b"newer")
    output = prefix.with_suffix(".mkv")

    def processor(_task, _progress):
        output.write_bytes(b"finished-output")
        source.unlink()
        if fails:
            raise RuntimeError("后续片段失败")
        return [output]

    queue = PostProcessQueue(tmp_path / "queue.json", processor=processor, autostart=False)
    queue.enqueue_subtitles(prefix, jsonl, [source])
    dialog = library.RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue)
    dialog.timer.setInterval(20)
    played = []
    monkeypatch.setattr(library.QDesktopServices, "openUrl",
                        lambda url: played.append(Path(url.toLocalFile())))
    try:
        _wait(qapp, lambda: dialog.library_table.rowCount() == 2)
        dialog.search.setText("直播")
        dialog.library_table.selectRow(1)
        assert dialog._selected_session().prefix == prefix
        queue.start()
        assert queue.wait_until_idle(3)
        # 后台扫描尚未送达时，播放也不能打开已删除的原文件。
        dialog.play_btn.click()
        assert played == [output]
        _wait(qapp, lambda: dialog._selected_session().videos == (output,))
        assert dialog.search.text() == "直播"
        assert dialog._selected_session().total_bytes == output.stat().st_size + jsonl.stat().st_size
        dialog.play_btn.click()
        assert played == [output, output]
    finally:
        dialog.reject()
        queue.shutdown()


def test_completion_during_scan_is_not_lost_and_close_stops_refresh(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "20261020_200000_直播"
    source, output = prefix.with_suffix(".mp4"), prefix.with_suffix(".mkv")
    source.write_bytes(b"source")
    jsonl = prefix.with_suffix(".jsonl")
    jsonl.write_text("{}", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    scan = library.scan_library
    scans = []

    def slow_scan(root):
        snapshot = scan(root)
        scans.append(snapshot)
        if len(scans) == 1:
            entered.set()
            assert release.wait(3)
        return snapshot

    def processor(_task, _progress):
        source.replace(output)
        return [output]

    monkeypatch.setattr(library, "scan_library", slow_scan)
    queue = PostProcessQueue(tmp_path / "queue.json", processor=processor, autostart=False)
    queue.enqueue_subtitles(prefix, jsonl, [source])
    dialog = library.RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue)
    try:
        assert entered.wait(3)
        queue.start()
        assert queue.wait_until_idle(3)
        dialog._refresh_queue()
        release.set()
        _wait(qapp, lambda: bool(dialog.sessions)
              and next(iter(dialog.sessions.values())).videos == (output,))
        assert len(scans) == 2
        dialog.reject()
        assert not dialog.timer.isActive()
        dialog.refresh()
        assert len(scans) == 2
    finally:
        release.set()
        dialog.reject()
        queue.shutdown()


def test_missing_video_does_not_play_a_different_session(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "20261020_200000_直播"
    source = prefix.with_suffix(".mp4")
    source.write_bytes(b"source")
    (tmp_path / (prefix.name + "精彩.mp4")).write_bytes(b"different session")
    queue = PostProcessQueue(tmp_path / "queue.json", autostart=False)
    dialog = library.RecordingLibraryDialog(AppSettings(out_dir=str(tmp_path)), queue)
    played, warnings = [], []
    monkeypatch.setattr(library.QDesktopServices, "openUrl", lambda url: played.append(url))
    monkeypatch.setattr(library.QMessageBox, "information",
                        lambda *_args: warnings.append(True))
    try:
        _wait(qapp, lambda: dialog.library_table.rowCount() == 2)
        dialog.library_table.selectRow(1)
        assert dialog._selected_session().prefix == prefix
        source.unlink()
        dialog.play_btn.click()
        assert played == []
        assert warnings == [True]
    finally:
        dialog.reject()
        queue.shutdown()
