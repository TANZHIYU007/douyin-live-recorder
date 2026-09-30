"""多房间顺序、缓冲上限和启动中停止的保护。"""

import threading

from dylive import manager
from dylive.recorder import Options, Status


def test_rooms_keep_insertion_order_without_duplicate_state():
    rooms = manager.RoomManager(lambda rid: Options(target=rid))
    first = rooms.add("123")
    second = rooms.add("456")
    assert rooms.add("123") is first
    assert rooms.rooms() == [first, second]
    rooms.remove("123")
    assert first._stop_requested.is_set()
    new = rooms.add("123")
    assert rooms.rooms() == [second, new]
    assert new is not first
    rooms.remove("missing")


def test_buffer_remains_bounded_and_drains_in_order():
    buffer = manager.RoomBuffer(limit=3)
    for item in range(5):
        buffer.write(item)
    assert buffer.drain(2) == [2, 3]
    assert buffer.drain() == [4]
    buffer.write(5)
    buffer.clear()
    assert buffer.drain() == []


def test_stop_during_construction_is_not_lost(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class FakeRecorder:
        def __init__(self, opts, hub):
            self.extra_writers = []
            entered.set()
            assert release.wait(3)

        def stop(self):
            calls.append("stop")

        def run(self):
            calls.append("run")

        def status(self):
            return Status(counts={"chat": 2})

    monkeypatch.setattr(manager, "Recorder", FakeRecorder)
    room = manager.Room("123", None, lambda rid: Options(target=rid))
    try:
        room.start()
        assert entered.wait(3)
        room.start()  # 重复点击不创建第二个录制器
        room.stop()
    finally:
        release.set()
        room.join(3)
    assert calls == ["stop", "run"]
    assert not room.active and not room.error
    assert room.totals == {"chat": 2}
