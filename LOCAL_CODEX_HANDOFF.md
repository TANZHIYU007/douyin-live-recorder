# 本地 Codex 交接说明

迁移日期：2026-11-13（Asia/Shanghai）。仓库：TANZHIYU007/douyin-live-recorder。
基线提交：`7aa73b03e92a0d356410621f63382c21a28251a5`。迁移分支：`handoff/speech-models-20261113`。

## 先读这里

当前代码是在 main 的 v1.3.0 基础上，新增了本地语音转文字功能。此前的改动保留在
云端工作区，尚未推送到 GitHub。本迁移包额外在临时导出仓库中创建了一次快照提交，
让你在本地能直接从完整 Git 分支继续；没有改变云端原工作分支，也没有推送远端。

## 用户要求与已经实现的功能

- 只手动转写选中的直播场次，不自动转写每段录像。
- 设置中选择、下载、删除或离线导入本地模型。保留 Whisper Tiny/Base/Small/Medium/Large-v3。
- 新增阿里 SenseVoice Small 和 Paraformer 中文 ONNX；直接走魔搭官方 CDN，固定版本及 SHA-256 校验。
- 独立语音 SRT、弹幕 ASS，并把两条独立轨及一条兼容合并轨封入新 MKV。语音底部居中，弹幕避开底部。
- 保留原视频与原字幕轨；处理队列支持取消、失败重试、分片完成检查点。
- 内置回放可分别关闭语音与弹幕，切片后保持开关选择。
- 转写运行在独立进程，避免与 Qt 播放器的原生库相互影响。长音频按五分钟窗口处理。
- 修正过回放后台加载 QObject 的所有权与窗口关闭后的回调处理。

## 重点代码

- `dylive/transcription.py`：模型目录、固定版本下载、校验、离线导入、转写及 SRT。
- `dylive/china_asr.py`：国产 ONNX 模型的 CPU 特征提取和识别适配；没有 PyTorch/魔搭 SDK 依赖。
- `dylive/speech_worker.py`：识别子进程与取消清理，打包后由 EXE 的 --speech-worker 分支启动。
- `dylive/speech_subtitle.py`：独立字幕轨及兼容轨，无损封装与输出保护。
- `dylive/processing.py`：处理队列和转写任务持久化。
- `dylive/ui/transcription.py`、`settings.py`、`library.py`、`replay.py`：相关 GUI。
- `build_exe.py`、`build_app.py`、`gui.py`：语音原生依赖打包与成品自检。

## 已验证与局限

上次完成开发时，完整回归 **403 项通过**；此迁移阶段没有重新运行整套回归。
真实中文直播样片：SenseVoice、Paraformer 各生成 10 段字幕，Whisper Tiny 生成 15 段。
验证了旧弹幕字幕处理后再加入语音的共存、四条字幕轨的保留、音视频完整解码和原文件哈希不变。
三种模型在实际 Qt 回放同时运行时均成功，界面计时器继续响应，GUI 不加载推理/PyAV 库。
旧真实录制再次得到约 34.5 秒视频、46 帧弹幕数据、85 条事件（其中 2 条聊天）。
Linux 冻结运行库及冻结子进程识别通过；**未生成或实际使用新的 Windows/macOS 成品**。
大陆 ISP 下载速度没有实测，不能从海外云节点结果推断。国产字幕时间是 VAD 语音段，不是逐字对齐。
更多历史证据在迁移包顶层 `evidence/`，其中绝对路径仅描述原云环境。

## 下一项未完成任务（优先）

用户明确要求：**生成 Windows 安装包，再以普通用户视角实际使用，不能用源码直接运行来代替成品测试。**
公开直播间可以自行选择。应从安装或解压后的 EXE 开始，检查首次启动、设置滚动、模型下载/
导入、真实画面录制、弹幕落盘、单场语音转写、字幕开关、重启后的配置/队列，以及截图和日志。
现有 build_exe.py 生成 Full/Lite 免安装 EXE；如提供安装向导，还需制作和验证安装/卸载包。
此前云端 Windows 构建准备被执行服务反复断连阻断：没有创建 Windows QA 工作流、没有推送
测试分支、没有运行 Windows Actions、没有生成新的安装包。不要把 Linux 源码测试算成 Windows 成品验收。
用户没有要求发布新版本，先保留为本地/测试构建；不要为了构建而创建发布标签或 GitHub Release。

## Windows 开发环境建议（PowerShell）

安装 Python 3.12、Git、FFmpeg/FFprobe。项目内执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "PySide6==6.8.3" ".[dev]" "pyinstaller>=6.11" "pyinstaller-hooks-contrib>=2025.1"
.\.venv\Scripts\python.exe -m playwright install chromium
```

PySide6 6.8.3 是本次云端验证使用的版本。保留项目 av>=11,<17 的兼容约束。
安装模型用设置中的魔搭下载/导入入口；不要把模型权重、缓存或用户录像提交到 Git。

源码回归与构建（构建阶段可以运行脚本，用户体验阶段必须启动成品）：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:LUMINA_TEST_FFMPEG = (Get-Command ffmpeg).Source
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -ra
Remove-Item Env:QT_QPA_PLATFORM
.\.venv\Scripts\python.exe build_exe.py
```

FFmpeg 需是真实可执行文件，FFprobe 在同目录；系统若使用启动 shim，请设置 LUMINA_FFMPEG_DIR
为真实二进制目录（README 有说明）。构建产物预计在 dist/Lumina-Full.exe 与 Lumina-Lite.exe。
软件可继续从源码开发，但不能以源码 GUI 冒充用户要求的 Windows 成品体验结果。
