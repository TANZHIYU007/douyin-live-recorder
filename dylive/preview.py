"""实时画面预览。

用一个**独立**的 ffmpeg 进程把流转成 MJPEG 帧喂给界面，不碰录制那一路。

为什么不复用录制的 ffmpeg 多加一路输出：那样只要界面这边读慢了，管道一堵，
ffmpeg 会连带卡住录制那一路。录像是主要目的，预览是锦上添花，两者不该有
任何耦合。

代价是多拉一份流，所以预览刻意拉**最低画质**（标清通常只有几百 kbps），
再缩到界面用得上的宽度，带宽和 CPU 都可以忽略。

管道读取有个坑，见 _read_frames 的注释 —— 早先的版本因为它实际只跑到
1.5 fps，看着一卡一卡的。
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable, List, Optional, Tuple

from .utils import UA
from .video import NO_WINDOW

log = logging.getLogger("preview")

SOI = b"\xff\xd8"       # JPEG 开始
EOI = b"\xff\xd9"       # JPEG 结束
MAX_BUFFER = 8 << 20    # 攒了这么多还没凑齐一帧就说明流坏了，清掉重来
CHUNK = 1 << 16

FOLLOW_SOURCE = 0       # fps 传 0 表示不限帧，直播源多少就多少


class PreviewStream:
    """把一路直播流转成 JPEG 帧。

    两条线程分工：读取线程只负责从管道里切出完整的 JPEG，绝不做解码这种
    慢活；派发线程把最新一帧交给 on_frame 回调（界面在那里解码）。分开是
    为了让读取永远跟得上 —— 读慢了管道就堵，ffmpeg 跟着卡，画面就开始跳。

    没有回调时退化成轮询模式，latest() 随时取最新一帧。
    """

    def __init__(self, ffmpeg: str, url: str, width: int = 640,
                 fps: int = FOLLOW_SOURCE,
                 on_frame: Optional[Callable[[bytes, int], None]] = None):
        self.ffmpeg = ffmpeg
        self.url = url
        self.width = width
        self.fps = fps
        self.on_frame = on_frame

        self.frames = 0
        self.dropped = 0        # 界面没跟上、被跳过的帧数
        self.error = ""
        self._latest: Optional[bytes] = None
        self._seq = 0          # 已产出的帧数
        self._consumed = 0     # 界面已经拿走的帧号
        self._cond = threading.Condition()
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # -- 生命周期 ---------------------------------------------------------

    def command(self) -> List[str]:
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-user_agent", UA,
            "-headers", "Referer: https://live.douyin.com/\r\n",
            "-fflags", "nobuffer", "-flags", "low_delay",
            # 默认要嗅探 5MB / 5 秒才开始出画，预览等不起；直播流就一路
            # H.264，用不着分析那么久
            "-probesize", "1000000", "-analyzeduration", "1000000",
            "-rw_timeout", "15000000",
            "-i", self.url,
            "-an",                                  # 预览不需要声音
            # 只缩小不放大：标清源本来就没几个像素，硬拉大只会糊，还白烧 CPU
            "-vf", "scale=w='min(%d,iw)':h=-2" % self.width,
        ]
        if self.fps > 0:
            cmd += ["-r", str(self.fps)]
        else:
            # 不限帧时明确要求原样透传，否则 mjpeg 复用器会按固定帧率
            # 补帧，白白多出一堆和上一张一模一样的图
            cmd += ["-fps_mode", "passthrough"]
        cmd += ["-q:v", "6", "-f", "mjpeg", "pipe:1"]
        return cmd

    def start(self) -> None:
        if self._proc is not None:
            return
        self._stop.clear()
        try:
            self._proc = subprocess.Popen(
                self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, **NO_WINDOW)
        except OSError as exc:
            self.error = "预览启动失败：%s" % exc
            log.warning("%s", self.error)
            return
        targets = [(self._read_frames, "read"), (self._read_errors, "err")]
        if self.on_frame is not None:
            targets.append((self._dispatch, "push"))
        for target, name in targets:
            t = threading.Thread(target=target, name="preview-" + name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()         # 叫醒还在等新帧的派发线程
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()                     # 预览没有需要收尾的文件，直接杀
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for t in self._threads:
            t.join(timeout=3)
        self._threads.clear()
        with self._cond:
            self._latest = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def latest(self) -> Optional[bytes]:
        with self._cond:
            return self._latest

    def snapshot(self) -> Tuple[Optional[bytes], int]:
        """最新一帧和它的序号。序号没变就说明画面还是老的，不必重画。"""
        with self._cond:
            return self._latest, self._seq

    # -- 读取 -------------------------------------------------------------

    def _publish(self, jpeg: bytes) -> None:
        with self._cond:
            if self._seq > self._consumed:
                self.dropped += 1       # 上一帧界面还没取走就被顶掉了
            self._latest = jpeg
            self._seq += 1
            self.frames += 1
            self._cond.notify_all()

    def _read_frames(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buffer = bytearray()
        while not self._stop.is_set():
            # read1 而不是 read：read(n) 会一直阻塞到**凑满** n 个字节。
            # 一帧标清 JPEG 才一两万字节，凑满 64KB 要等三四帧，而我们只留
            # 缓冲区里最后一张完整的图 —— 中间那几帧就这么被丢掉了。实测
            # 限帧 5fps 的情况下真实只剩 1.5fps，画面一卡一卡的根源就在这。
            chunk = proc.stdout.read1(CHUNK)
            if not chunk:
                break
            buffer += chunk

            # 一次读回来可能不止一帧，全部按顺序发出去，别只留最后一张
            while True:
                start = buffer.find(SOI)
                if start < 0:
                    if len(buffer) > MAX_BUFFER:
                        log.debug("预览缓冲异常，重置")
                        buffer.clear()
                    break
                end = buffer.find(EOI, start + 2)
                if end < 0:
                    del buffer[:start]      # 帧头之前的垃圾丢掉，等剩下的字节
                    if len(buffer) > MAX_BUFFER:
                        buffer.clear()
                    break
                self._publish(bytes(buffer[start:end + 2]))
                del buffer[:end + 2]

    def _dispatch(self) -> None:
        """把新帧推给界面。解码在回调里做，所以必须是独立线程。"""
        seen = 0
        while not self._stop.is_set():
            with self._cond:
                while self._seq == seen and not self._stop.is_set():
                    self._cond.wait(0.5)
                if self._stop.is_set():
                    return
                jpeg, seen = self._latest, self._seq
                self._consumed = seen
            if jpeg and self.on_frame is not None:
                try:
                    self.on_frame(jpeg, seen)
                except Exception:           # 界面出错不能带死预览
                    log.debug("预览回调异常", exc_info=True)

    def _read_errors(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line and not self._stop.is_set():
                self.error = line
                log.debug("预览 ffmpeg: %s", line)
