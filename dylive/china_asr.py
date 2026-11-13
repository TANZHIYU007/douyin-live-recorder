"""阿里官方 ONNX 模型的纯本地 CPU 适配，不下载模型或加载远程代码。

特征约定遵循 FunASR (MIT): https://github.com/modelscope/FunASR
使用 Kaldi 原生 fbank、LFR 拼帧和模型自带 CMVN；不依赖 PyTorch/FunASR SDK。
字幕时间为 VAD 语音片段范围，并非逐字强制对齐。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace


class ChineseRecognizer:
    def __init__(self, directory: Path, engine: str):
        import kaldi_native_fbank as knf
        import numpy as np
        import onnxruntime as ort
        import yaml

        self.engine = engine
        self.tokens = json.loads((directory / "tokens.json").read_text(encoding="utf-8"))
        config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
        front = config["frontend_conf"]
        options = knf.FbankOptions()
        options.frame_opts.samp_freq = 16000
        options.frame_opts.dither = 0
        options.frame_opts.window_type = front.get("window", "hamming")
        options.frame_opts.frame_shift_ms = float(front.get("frame_shift", 10))
        options.frame_opts.frame_length_ms = float(front.get("frame_length", 25))
        options.frame_opts.snip_edges = True
        options.mel_opts.num_bins = int(front.get("n_mels", 80))
        options.energy_floor = 0
        self.options = options
        self.lfr_m, self.lfr_n = int(front.get("lfr_m", 1)), int(front.get("lfr_n", 1))
        self.bias = int(config.get("model_conf", {}).get("predictor_bias", 0))
        lines = (directory / "am.mvn").read_text().splitlines()
        stats = {}
        for index, line in enumerate(lines[:-1]):
            for key in ("<AddShift>", "<Rescale>"):
                if line.strip().startswith(key):
                    row = lines[index + 1]
                    stats[key] = np.fromstring(row[row.index("[") + 1:row.index("]")], sep=" ")
        self.shift, self.scale = stats["<AddShift>"], stats["<Rescale>"]
        options_ort = ort.SessionOptions()
        options_ort.intra_op_num_threads = min(4, os.cpu_count() or 1)
        options_ort.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model_quant.onnx"),
            sess_options=options_ort, providers=["CPUExecutionProvider"])

    def _features(self, audio):
        import kaldi_native_fbank as knf
        import numpy as np

        fbank = knf.OnlineFbank(self.options)
        fbank.accept_waveform(16000, (audio * 32768).tolist())
        count = fbank.num_frames_ready
        if not count:
            return None
        features = np.stack([fbank.get_frame(index) for index in range(count)])
        frames = (count + self.lfr_n - 1) // self.lfr_n
        left = (self.lfr_m - 1) // 2
        padded = np.pad(features, ((left, self.lfr_m), (0, 0)), mode="edge")
        lfr = np.stack([padded[i * self.lfr_n:i * self.lfr_n + self.lfr_m].reshape(-1)
                        for i in range(frames)])
        return ((lfr + self.shift) * self.scale).astype(np.float32)[None]

    def _decode(self, audio, language):
        import numpy as np

        features = self._features(audio)
        if features is None:
            return ""
        values = [features, np.array([features.shape[1]], dtype=np.int32)]
        if self.engine == "sensevoice":
            lid = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12}
            values += [np.array([lid.get(language or "auto", 0)], dtype=np.int32),
                       np.array([14], dtype=np.int32)]
        names = [item.name for item in self.session.get_inputs()]
        if len(names) != len(values):
            raise RuntimeError("模型输入格式不匹配，请重新下载所选模型")
        outputs = self.session.run(None, dict(zip(names, values, strict=True)))
        if self.engine == "sensevoice":
            ids = np.argmax(outputs[0][0, :int(outputs[1][0])], axis=-1).tolist()
            ids = [token for i, token in enumerate(ids) if token and (i == 0 or ids[i - 1] != token)]
        else:
            length = max(0, int(outputs[1][0]) - self.bias)
            ids = np.argmax(outputs[0][0], axis=-1).tolist()[:length]
        tokens = [self.tokens[token] for token in ids if 0 <= token < len(self.tokens)]
        tokens = [token for token in tokens if not (token.startswith("<") and token.endswith(">"))]
        text = ""
        for token in tokens:
            if self.engine == "paraformer" and text and token and text[-1].isascii() and token[0].isascii():
                text += " "
            text += token
        return re.sub(r"\s+", " ", text.replace("▁", " ").replace("@@ ", "").replace("@@", "")).strip()

    def transcribe(self, waveform, language=None, **_kwargs):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        if self.engine == "paraformer" and language not in (None, "auto", "zh"):
            raise ValueError("Paraformer 中文模型仅支持普通话，请选择中文或自动识别")
        spans = get_speech_timestamps(waveform, VadOptions(
            min_speech_duration_ms=150, min_silence_duration_ms=400,
            speech_pad_ms=150, max_speech_duration_s=12))

        def segments():
            for span in spans:
                text = self._decode(waveform[span["start"]:span["end"]], language)
                if text:
                    yield SimpleNamespace(start=span["start"] / 16000,
                                          end=span["end"] / 16000, text=text)
        return segments(), SimpleNamespace(language=language or ("zh" if self.engine == "paraformer" else "und"))
