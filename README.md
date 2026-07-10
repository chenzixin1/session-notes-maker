# Session Notes Maker 使用文档

![Session Notes Maker 项目概览：自动选择火山 ASR 或本地 Whisper 转录，Codex sub-agent 并行校对和轻量打磨](assets/session-notes-maker-overview.png)

这个 Skill 用来把一段演讲、课程、会议或带 PPT 的视频，自动生成一份可分享的 HTML 图文稿。最终产物包含：

- 视频转录稿
- PPT/幻灯片截图
- 幻灯片与转录稿对齐后的 Markdown
- 轻量打磨后的文章
- PC 宽屏 HTML 页面
- 只包含实际引用图片的分享目录
- 可选 zip 压缩包

本 Skill 是**可单独分发**的，所需脚本已经内置在 `scripts/` 目录中，不依赖宿主项目根目录里的 `01_transcribe_video.py`、`02_extract_slide_timestamps.py` 等文件。

---

## 处理流程总览

```mermaid
flowchart TD
    inputVideo["输入视频<br/>video.mp4"] --> extractAudio["01 抽取音频<br/>16kHz 单声道 MP3"]
    extractAudio --> provider{"存在有效的<br/>VOLCENGINE_API_KEY？"}
    provider -->|是| volcengine["火山 ASR Flash<br/>Base64 直接发送"]
    provider -->|否| whisper["本地 Whisper<br/>默认 small 模型"]
    volcengine --> transcript["统一转录稿<br/>video_transcript.txt"]
    whisper --> transcript

    inputVideo --> slideDetect["02 截图与换页检测<br/>框选 PPT 区域并生成截图"]
    slideDetect --> slideMd["幻灯片时间戳 Markdown<br/>video.md"]
    slideDetect --> slideImages["原始截图目录<br/>ppt_pics"]

    transcript --> integrate["03 整合<br/>按时间戳匹配转录稿与幻灯片"]
    slideMd --> integrate
    integrate --> integratedMd["整合稿<br/>video_integrated.md"]

    integratedMd --> polish["05 轻量打磨<br/>light-plus 模式"]
    slideImages --> polish
    polish --> processedMd["终稿 Markdown<br/>video_integrated_Processed.md"]

    processedMd --> shareBuild["构建分享目录<br/>重命名图片目录并改写链接"]
    slideImages --> shareBuild
    shareBuild --> cleanup["清理图片<br/>只保留 Markdown 实际引用的图片"]
    cleanup --> htmlBuild["Pandoc 转 HTML<br/>PC 宽屏样式"]
    htmlBuild --> shareFolder["分享目录<br/>share/"]

    shareFolder --> optionalCompress["可选 PNG 压缩<br/>pngquant + oxipng"]
    optionalCompress --> zipPackage["可选打包<br/>video_html_share.zip"]
    shareFolder --> zipPackage
```

---

## 1. 目录结构

```text
session-notes-maker/
├── SKILL.md
├── README.md
└── scripts/
    ├── 00_build_session_notes.py
    ├── 01_transcribe_video.py
    ├── 02_extract_slide_timestamps.py
    ├── 03_integrate_transcript_slides.py
    ├── 04_polish_slide_transcript.py
    ├── markdown_llm_utils.py
    ├── 05_compress_png_images.py
    ├── config.example.py
    ├── requirements.txt
    ├── requirements-whisper.txt
    └── .gitignore
```

核心入口是：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py
```

---

## 2. 安装部署

### 2.1 放置 Skill

推荐作为个人 Skill 单独分发，将目录放到：

```text
~/.cursor/skills/session-notes-maker/
```

如果需要作为某个项目的项目级 Skill，也可以放到：

```text
<your-project>/.cursor/skills/session-notes-maker/
```

下文默认使用个人 Skill 路径：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py ...
```

### 2.2 安装 Python 依赖

建议使用 Python 3.10+。在 Skill 所在环境中执行：

```bash
pip install -r ~/.cursor/skills/session-notes-maker/scripts/requirements.txt
```

`requirements.txt` 包含：

- `requests`
- `moviepy`
- `opencv-python`
- `scikit-image`
- `numpy`
- `Pillow`
- `openai`

如果希望在没有火山 Key 时自动使用本地 Whisper，再安装可选依赖：

```bash
pip install -r ~/.cursor/skills/session-notes-maker/scripts/requirements-whisper.txt
```

首次使用某个 Whisper 模型时会自动下载模型文件；默认使用兼顾速度与准确率的 `small`。

### 2.3 安装系统命令

还需要以下命令行工具：

```bash
brew install ffmpeg pandoc pngquant oxipng
```

用途：

- `ffmpeg`：从视频中抽取音频，供 MoviePy 使用。
- `pandoc`：把 Markdown 转换成 standalone HTML。
- `pngquant`：压缩 PNG，但仍保持 PNG 格式。
- `oxipng`：进一步无损优化 PNG。

如果不使用 `--compress-png`，`pngquant` 和 `oxipng` 可以不装。

---

## 3. 配置文件

Skill 内置了配置模板：

```text
scripts/config.example.py
```

复制一份作为真实配置：

```bash
cp ~/.cursor/skills/session-notes-maker/scripts/config.example.py \
   ~/.cursor/skills/session-notes-maker/scripts/config.py
```

`VOLCENGINE_API_KEY` 是可选项：配置有效 Key 时优先调用火山引擎；未配置时自动使用本地 Whisper。Codex sub-agent 使用当前 Codex 会话，不需要额外模型 API Key。

注意：

- `scripts/config.py` 包含真实密钥，不要分享。
- `scripts/.gitignore` 已经忽略 `config.py`。
- 分发 Skill 时只应分发 `config.example.py`。

---

## 4. 需要哪些模型和服务

默认 Codex 路径不强制依赖外部模型服务。转录脚本会自动选择后端：有有效的火山 Key 时使用火山引擎，未配置 Key 时使用本地 Whisper。

图片理解、术语校对和 `light-plus` 轻量打磨由当前 Codex 会话中的多个 sub-agent 并行完成，不需要 OpenRouter，也不需要额外模型 API Key。

### 4.1 Codex sub-agent

Codex 会把逐页 PPT 截图和对应转录稿拆成互不重叠的批次，交给多个 sub-agent 并行生成 `codex_notes/slide_N.md`，再由本地脚本汇总。最终稿严格保持一页 PPT 对应一段完整讲稿。

### 4.2 转录后端

默认 `--provider auto`：

- 配置有效 `VOLCENGINE_API_KEY`：使用火山引擎 ASR Flash；
- 未配置 Key：使用本地 OpenAI Whisper；
- 也可用 `--provider volcengine|whisper` 显式指定。

本地 Whisper 默认模型为 `small`，可通过 `--whisper-model tiny|base|small|medium|large-v3` 调整，并可用 `--whisper-device auto|cpu|cuda` 指定设备。

火山引擎配置项：

```python
VOLCENGINE_API_KEY = "YOUR_VOLCENGINE_API_KEY"
RECOGNIZE_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"
DEFAULT_LANGUAGE = "zh-CN"
```

脚本使用录音文件极速版 HTTP 接口。本地 MP3 会以 Base64 放入 `audio.data` 直接发送，一次请求返回结果，不需要临时对象存储或异步轮询。单文件限制为 2 小时、100MB，支持 WAV、MP3 和 OGG OPUS。

---

## 5. 可选的云端 API

### 5.1 申请火山引擎语音识别（可选）

步骤：

1. 注册并登录火山引擎账号。
2. 开通语音识别 / 大模型语音识别相关服务。
3. 在新版控制台获取可调用语音识别接口的 API Key，并开通 `volc.bigasr.auc_turbo` 资源。
4. 确认账号有调用额度、计费方式和权限。
5. 将 key 填入：

```python
VOLCENGINE_API_KEY = "YOUR_VOLCENGINE_API_KEY"
```

默认接口地址：

```python
RECOGNIZE_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"
```

注意：

- 新版控制台只需要 `VOLCENGINE_API_KEY`。
- 旧版控制台需要同时配置 `APP_KEY` 与 `ACCESS_KEY`。
- 音频不经过第三方存储；Base64 内容只发送到火山引擎识别接口。
- 不配置火山 Key 时无需申请 API，安装 `requirements-whisper.txt` 后会自动在本机转录。

---

## 6. API 是怎么调用的

### 6.1 转录调用链路

```text
video.mp4
  -> 抽取 audio.mp3
  -> 自动选择转录后端
     -> 有有效 Key：Base64 直传火山引擎 recognize/flash API
     -> 无 Key：本地 Whisper 识别
  -> 写出 <video>_transcript.txt
```

对应脚本：

```text
scripts/01_transcribe_video.py
```

### 6.2 图片理解和文字打磨调用链路

```text
每一页幻灯片截图 + 对应转录文本
  -> Codex 拆分互不重叠的批次
  -> 多个 Codex sub-agent 并行校对与轻量打磨
  -> codex_notes/slide_N.md
  -> 本地汇总为一页 PPT 对应一段完整讲稿
```

对应脚本：

```text
scripts/04_polish_slide_transcript.py
scripts/markdown_llm_utils.py
```

本 Skill 固定使用 `light-plus` 模式：

- 以转录稿为主体；
- 幻灯片只用于纠错、补充必要上下文；
- 不做大幅重写；
- 最终 HTML 不包含 prompt 附录。

在 Codex 中可使用 `scripts/05_prepare_codex_batches.py` 把逐页输入拆成互不重叠的批次，交给多个 sub-agent 并行生成 `codex_notes/slide_N.md`，再由 `--provider codex-notes` 汇总。这是 Codex 本地执行的 light-plus 路径，不依赖 OpenRouter。

逐页校正完成后，使用 `scripts/06_merge_light_polish_scenes.py` 把每页 PPT 内的字幕碎片合并成一段完整讲稿。最终稿严格保持一页 PPT 对应一段讲稿，不跨页合并；正文使用自然段，不保留逐条 ASR 时间戳，时间只在 PPT 标题中保留整体范围。

---

## 7. 使用方法

### 7.1 第一次处理单个视频

如果还没有 PPT 截图区域配置，建议使用交互模式：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py \
  "/path/to/video.mp4" \
  --interactive \
  --zip
```

交互窗口中：

1. 用鼠标框选 PPT 区域；
2. 按 Enter 或空格确认；
3. 脚本会保存 `<video>.ppt_rect.json`，下次可复用。

### 7.2 使用已有截图区域

如果视频旁边已有：

```text
<video>.ppt_rect.json
```

可以直接运行：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py \
  "/path/to/video.mp4" \
  --zip
```

### 7.3 显式指定截图区域

如果已知相对坐标：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py \
  "/path/to/video.mp4" \
  --ppt-rect "0.0359,0.0806,0.6792,0.7296" \
  --zip
```

### 7.4 抽帧速度模式

`02_extract_slide_timestamps.py` 默认使用 hybrid-keyframe 模式：

1. 先只解 H.264 keyframes，用低清灰度图快速召回 slide 变化候选；
2. 对连续候选附近做局部 2 秒网格 SSIM 精修，过滤动画/转场尾巴；
3. 只为精修后的 slide 起点并行导出高清图片。

当 PPT 区域固定时，hybrid-keyframe 是默认路径。默认检测宽度是 `--detect-width 240`，默认图片格式是 `--image-format png`，优先保证 PPT 文字、线条和图表清晰度。

如果某个视频编码不适合 keyframe 召回，可使用 `--detection-backend accurate` 回到完整 2 秒网格扫描。完整 39:10 样本基准中，accurate 路径用时 `34.79s`，hybrid-keyframe + PNG 默认路径用时 `9.34s`，输出同样为 `28` 个 slide 起点，速度约为 `3.72x`。如果需要更小体积或极限速度，可显式使用 `--image-format jpg --jpeg-quality 90`。

### 7.5 变更日志

- `2026-07-01`: 默认抽帧路径改为 `hybrid-keyframe`，结合 keyframe 快速召回和局部 accurate 精修。
- `2026-07-01`: 默认截图格式保持 PNG，优先保证 PPT 文字和线条质量；JPG/WebP 作为可选小体积/极速模式。
- `2026-07-01`: 保留 `--detection-backend accurate`，用于需要完整 2 秒网格扫描的保守场景。
- `2026-07-01`: HTML helper 的图片引用清理从只支持 PNG 扩展为支持 PNG/JPG/JPEG/WebP。

### 7.6 压缩 PNG 后打包

如果需要保持 PNG 格式但压缩体积：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py \
  "/path/to/video.mp4" \
  --ppt-rect "0.0359,0.0806,0.6792,0.7296" \
  --compress-png \
  --zip
```

---

## 8. 输出结果

默认输出目录：

```text
<video_parent>/<video_stem>_html_output/
```

结构：

```text
<video_stem>_html_output/
├── work/
│   ├── <video_stem>.md
│   ├── <video_stem>_integrated.md
│   ├── <video_stem>_integrated_Processed.md
│   └── ppt_pics/
├── share/
│   ├── <video_stem>.html
│   ├── <video_stem>.md
│   └── <video_stem>_ppt_pics/
└── <video_stem>_html_share.zip
```

其中最适合分享的是：

```text
<video_stem>_html_output/<video_stem>_html_share.zip
```

如果不打包，则分享整个 `share/` 目录。

---

## 9. 常见问题

### 9.1 HTML 打开后图片不显示

检查：

- HTML 是否和 `<video_stem>_ppt_pics/` 在同一个 `share/` 目录；
- 图片目录是否被一起复制；
- 文件路径中是否被手动改名。

runner 会在结束前检查 `<img src="...">` 引用是否都能找到。

### 9.2 转录失败

常见原因：

- 火山引擎 `VOLCENGINE_API_KEY` 不正确；
- 未开通 `volc.bigasr.auc_turbo` 资源；
- 音频超过 2 小时或 100MB；
- 音频格式不属于 WAV、MP3、OGG OPUS；
- 服务额度不足或 HTTP 请求超时；
- 未配置火山 Key，同时未安装 `scripts/requirements-whisper.txt`；
- 本地 Whisper 首次下载模型失败，或系统缺少 `ffmpeg`。

强制使用本地 Whisper 排查时可运行：

```bash
python scripts/01_transcribe_video.py /path/to/video.mp4 \
  --provider whisper \
  --whisper-model small
```

### 9.3 Codex notes 没有生成或没有生效

检查：

- 是否先运行 `05_prepare_codex_batches.py`；
- 每个 sub-agent 是否只写自己负责的 `slide_N.md`；
- `--codex-notes-dir` 是否指向正确目录；
- 是否使用 `--polish-provider codex-notes` 汇总。

### 9.4 图片压缩后仍然很大

使用：

```bash
--compress-png
```

它会用 `pngquant` + `oxipng` 压缩 PNG，仍保持 `.png` 格式。对于幻灯片截图，通常可以显著降低体积。

---

## 10. 安全提醒

不要把以下文件提交或分发给别人：

```text
scripts/config.py
```

可以安全分发：

```text
scripts/config.example.py
```

真实 key 只应保存在本机或受信任的部署环境中。
