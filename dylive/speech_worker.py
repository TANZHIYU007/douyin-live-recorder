"""识别运行库隔离进程：Qt 播放器与 ASR 原生库不在同一进程加载。"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dylive import transcription
from dylive.paths import NO_WINDOW


def main(request_file: str, result_file: str) -> int:
    request = json.loads(Path(request_file).read_text(encoding="utf-8"))
    tempfile.tempdir = str(Path(request_file).parent)  # 取消后由父进程统一清理所有中间音频。
    def progress(value, message):
        print(json.dumps({"fraction": value, "message": message}, ensure_ascii=False), flush=True)
    try:
        result = transcription.transcribe_video(
            Path(request["source"]), request["model"], request["language"],
            ffmpeg=request["ffmpeg"], output=Path(request["output"]), progress=progress)
        data = {"segments": [asdict(segment) for segment in result.segments],
                "language": result.language, "duration": result.duration,
                "srt_path": str(result.srt_path)}
    except Exception as exc:
        data = {"error": str(exc)}
    path = Path(result_file)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return int("error" in data)


def run(source, model="small", language=None, *, ffmpeg="", output=None,
        progress=None, cancelled=None):
    """父进程不导入 ML 库；取消只停止自己启动的识别子进程。"""
    with transcription._model_lock(model), tempfile.TemporaryDirectory(prefix="lumina-asr-worker-") as folder:
        folder = Path(folder)
        request, result_path = folder / "request.json", folder / "result.json"
        target = Path(output) if output else Path(source).with_name(Path(source).stem + ".speech.srt")
        request.write_text(json.dumps({"source": str(Path(source).resolve()),
            "model": model, "language": language, "ffmpeg": ffmpeg,
            "output": str(target.resolve())}), encoding="utf-8")
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--speech-worker", str(request), str(result_path)]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), str(request), str(result_path)]
        messages = queue.Queue()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", errors="replace",
                start_new_session=os.name != "nt", **NO_WINDOW)
            def read_progress():
                for line in process.stdout:
                    messages.put(line)
            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            try:
                while process.poll() is None or not messages.empty():
                    transcription._check_cancel(cancelled)
                    try:
                        message = json.loads(messages.get(timeout=.1))
                    except (queue.Empty, ValueError):
                        continue
                    if progress and isinstance(message, dict) and "fraction" in message:
                        progress(float(message["fraction"]), str(message.get("message", "")))
                transcription._check_cancel(cancelled)
                if not result_path.is_file():
                    raise transcription.TranscriptionError(
                        "识别进程异常退出（退出码 %s），录制和回放不受影响。" % process.returncode)
                data = json.loads(result_path.read_text(encoding="utf-8"))
                if "error" in data:
                    raise transcription.TranscriptionError(data["error"])
                return transcription.TranscriptionResult(
                    tuple(transcription.Segment(**item) for item in data["segments"]),
                    data["language"], float(data["duration"]), Path(data["srt_path"]))
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        try:
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=5, **NO_WINDOW)
                        except (OSError, subprocess.SubprocessError):
                            process.terminate()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            process.kill()
                        else:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.wait(timeout=5)
                reader.join(timeout=2)
                process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
