"""用 ffmpeg 录直播画面。

只做 remux（-c copy），不转码：CPU 几乎不占，画质无损。
停止时往 ffmpeg 的 stdin 写一个 q，让它自己把 moov 写完 —— 直接 kill 的话
mp4 会变成不可播放的半成品。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import runtime
from .paths import NO_WINDOW             # noqa: F401 - 老的导入点，别人还在用
from .utils import UA

log = logging.getLogger("video")

# 容器 -> (ffmpeg 输出格式, 扩展名, 额外参数)
CONTAINERS = {
    # 分片 mp4：即使进程被强杀，已写下的部分依然能播
    "mp4": ("mp4", ".mp4", ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]),
    "flv": ("flv", ".flv", []),
    "ts": ("mpegts", ".ts", []),
    "mkv": ("matroska", ".mkv", []),
}


def find_ffmpeg(explicit: str = "") -> str:
    # 打包版自带一份，找不到才回落到系统 PATH
    path = explicit or runtime.bundled_ffmpeg() or shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "找不到 ffmpeg。请先安装并加入 PATH，"
            "或用 --ffmpeg 指定可执行文件路径。")
    return path


class VideoRecorder:
    """一路 ffmpeg 录制。start() 之后用 wait()/stop() 控制。"""

    def __init__(self, ffmpeg: str, url: str, out: Path, container: str = "mp4",
                 segment_seconds: int = 0, extra_args: Optional[List[str]] = None):
        if container not in CONTAINERS:
            raise ValueError("不支持的容器格式 %r，可选：%s"
                             % (container, "/".join(CONTAINERS)))
        self.ffmpeg = ffmpeg
        self.url = url
        self.container = container
        self.segment_seconds = segment_seconds
        self.extra_args = list(extra_args or [])
        self.out = self._out_path(out)
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self._stderr_thread: Optional[threading.Thread] = None

    def _out_path(self, out: Path) -> Path:
        _, ext, _ = CONTAINERS[self.container]
        base = out.with_suffix("")
        if self.segment_seconds > 0:
            # ffmpeg 的 segment 复用器按 %03d 自己编号
            return base.parent / (base.name + "_part%03d" + ext)
        return base.parent / (base.name + ext)

    def build_command(self) -> List[str]:
        fmt, _, container_args = CONTAINERS[self.container]
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
            "-user_agent", UA,
            "-headers", "Referer: https://live.douyin.com/\r\n",
            # 抖音的边缘节点偶尔抽风，让 ffmpeg 自己先重连几次
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "20000000",          # 20s 无数据就报错退出，交给外层重试
            "-i", self.url,
            "-c", "copy",
        ]
        # HLS 的音频是 ADTS 帧，塞进 mp4 前要转成 ASC 头；FLV 源不需要
        if self.container == "mp4" and ".m3u8" in self.url:
            cmd += ["-bsf:a", "aac_adtstoasc"]

        if self.segment_seconds > 0:
            cmd += ["-f", "segment",
                    "-segment_time", str(self.segment_seconds),
                    "-segment_format", fmt,
                    "-reset_timestamps", "1"]
            if container_args:
                cmd += ["-segment_format_options",
                        "movflags=+frag_keyframe+empty_moov+default_base_moof"]
        else:
            cmd += container_args + ["-f", fmt]

        cmd += self.extra_args + [str(self.out)]
        return cmd

    def start(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command()
        log.debug("ffmpeg 命令：%s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **NO_WINDOW,
        )
        self.started_at = time.time()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ffmpeg-stderr", daemon=True)
        self._stderr_thread.start()
        log.info("开始录制画面 -> %s", self.out.name)

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.warning("ffmpeg: %s", line)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 15.0) -> None:
        """优雅停止：先请 ffmpeg 自己收尾，超时再强杀。"""
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q\n")
                    self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg 未在 %.0fs 内退出，强制结束", timeout)
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        log.info("画面录制结束（%s），退出码 %s", self.out.name, self.proc.returncode)

    def files(self) -> List[Path]:
        """列出这次录制实际产出的文件。

        这里用前缀匹配而不是 glob —— 直播间标题里的 [ ] 是 glob 元字符，
        会让匹配结果莫名其妙。
        """
        if self.segment_seconds > 0:
            prefix = self.out.name.split("%")[0]
            return sorted(p for p in self.out.parent.iterdir()
                          if p.is_file() and p.name.startswith(prefix)
                          and p.suffix == self.out.suffix)
        return [self.out] if self.out.exists() else []
