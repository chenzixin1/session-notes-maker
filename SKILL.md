---
name: session-notes-maker
description: 将演讲、会议、课程或带 PPT 的视频转换成带幻灯片截图和转录文本的 HTML 文章。适用于用户要求处理视频、PPT 演讲视频、会议演讲、Presentation recording、从视频生成可分享 HTML、提取 PPT 截图并整合转录稿等场景。Skill 内置完整流水线脚本。
---

# Session Notes Maker

## 目标

使用本 Skill 可以把单个演讲视频转换成 会议视频学习资料生成流程中生成的同类产物：

- 带时间戳的幻灯片截图
- 带时间戳的转录稿
- 幻灯片与转录稿整合后的 Markdown
- `light-plus` 模式处理后的 Markdown
- 适合 PC 阅读宽度的 HTML
- 只包含 HTML 和实际引用 PNG 图片的分享目录
- 可选的 zip 压缩包

## 内置脚本

本 Skill 可单独分发。它在 `scripts/` 目录内置以下可执行脚本：

- `01_transcribe_video.py`
- `02_extract_slide_timestamps.py`
- `03_integrate_transcript_slides.py`
- `04_polish_slide_transcript.py`
- `markdown_llm_utils.py`
- `05_compress_png_images.py`
- `00_build_session_notes.py`
- `requirements.txt`

runner 会直接调用这些内置副本，不依赖宿主项目根目录下是否存在同名脚本。

安装 Python 依赖：

```bash
pip install -r ~/.cursor/skills/session-notes-maker/scripts/requirements.txt
```

如果在 Apple Silicon 上希望加速第 2 步的 SSIM 幻灯片去重比较，可额外安装 PyTorch MPS 依赖：

```bash
pip install "torch>=2.1" "pytorch-msssim>=1.0"
```

安装后 `02_extract_slide_timestamps.py` 默认会用 `--ssim_backend auto` 自动尝试 MPS；也可以显式传入 `--ssim_backend mps` 或用 `--ssim_backend cpu` 回到原 CPU 路径。

还需要安装以下命令行工具：

- `pandoc`：用于 Markdown 转 HTML
- `ffmpeg`：供 MoviePy 处理视频/音频
- `pngquant` 和 `oxipng`：用于可选的 PNG 压缩

## 配置

本 Skill 包含 `scripts/config.example.py`，作为 key 和配置文件的脱敏模板。它保留 prompt、模型和配置结构，但把敏感 key 替换成占位符。

在单独分发和本地使用时，先从模板创建本地密钥文件：

```bash
cp ~/.cursor/skills/session-notes-maker/scripts/config.example.py ~/.cursor/skills/session-notes-maker/scripts/config.py
```

然后填写：

- `API_KEY`：OpenRouter
- `VOLCENGINE_API_KEY`：火山引擎新版控制台的 API Key

转录脚本使用火山引擎录音文件极速版 HTTP 接口，把本地音频 Base64 编码后放入 `audio.data` 直接发送。无需对象存储、公网音频 URL，也无需 submit/query 轮询。旧版控制台鉴权可额外配置 `APP_KEY` 和 `ACCESS_KEY`。

极速版单文件限制为 2 小时、100MB，支持 WAV、MP3 和 OGG OPUS。超过限制时应先切分音频，或改用标准版 URL 提交接口。

不要分享或提交 `scripts/config.py`；它已经被 `scripts/.gitignore` 忽略。

内置脚本会从自己的 `scripts/` 目录导入 `config.py`，因此单独分发后的 Skill 只需要在本地补齐 `scripts/config.py`。

## 推荐命令

使用 helper 脚本：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" --interactive --zip
```

如果视频旁边已经有保存好的 PPT 截图区域 sidecar（`<video>.ppt_rect.json`），可以省略 `--interactive`：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" --zip
```

如果一批视频画面布局一致，可以复用已知截图区域：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" --ppt-rect "0.0359,0.0806,0.6792,0.7296" --zip
```

## 工作流程

1. **转录视频**
   - 如果视频旁边已有 `<video>_transcript.txt`，直接复用。
   - 否则运行 `01_transcribe_video.py`。
   - 脚本从视频抽取 16kHz 单声道 MP3，缓存后通过 `audio.data` 直传火山引擎 ASR Flash；响应在同一次 HTTP 请求中返回。

2. **生成幻灯片时间戳 Markdown**
   - 运行 `02_extract_slide_timestamps.py`。
   - 默认使用 streaming 模式：顺序解码采样帧，在内存中做 SSIM，只把检测到的幻灯片变化帧写入 `ppt_pics/`。
   - 如需回到旧行为（先保存所有采样 PNG，再二次读取做 SSIM），传入 `--legacy_extract_all`。
   - 如果没有可靠的 PPT 截图区域，使用 `--interactive` 让用户框选。
   - 重复运行时，优先复用已保存的 `<video>.ppt_rect.json` 或显式传入 `--ppt-rect`。

3. **整合转录稿**
   - 运行 `03_integrate_transcript_slides.py`。
   - 输入：第 2 步生成的 `<video>.md` 和 `<video>_transcript.txt`。
   - 输出：`<video>_integrated.md`。

4. **使用轻量打磨模式处理**
   - 固定使用 `light-plus`，不提供重写模式。
   - 运行 `04_polish_slide_transcript.py`，参数：
     - `--mode light-plus`
     - `--no-review`
     - `-y`
   - 输出：`<video>_integrated_Processed.md`。
   - 流水线会自动删除文件末尾的“处理过程中使用的提示（Prompts）”附录，最终 HTML 不包含提示词内容。

5. **构建可分享 HTML**
   - 把处理后的 Markdown 复制到分享目录，并命名为 `<video>.md`。
   - 把 `ppt_pics/` 复制为 `<video>_ppt_pics/`。
   - 将 Markdown 图片链接改写为 `<video>_ppt_pics/...`。
   - 只保留最终 Markdown 实际引用的 PNG 图片。
   - 使用 `pandoc` 转换为 standalone HTML。
   - 将 HTML 正文宽度调整为适合 PC 阅读的宽度（`max-width: 1100px`），并让图片自适应正文宽度。

6. **可选打包**
   - 如果用户希望更小的包且必须保持 PNG 格式，用 `05_compress_png_images.py` 原地压缩被引用的 PNG。
   - 使用 `zip -0` 打包分享目录，避免对 PNG 进行耗时的二次压缩。

## 输出结构

默认输出在：

```text
<video_parent>/<video_stem>_html_output/
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

## 质量检查

报告完成前必须检查：

- HTML 文件存在；
- 所有 `<img src="...">` 引用都能在分享目录内找到；
- 如果用户要求 PNG 格式，确认图片文件仍然是 PNG；
- 如果用户要求打包，报告最终 zip 路径和大小。

## 实践经验

- 转录稿驱动的文章固定使用 `light-plus`，因为它以转录稿为主，只用幻灯片辅助校对和轻度润色。
- 不要把所有截帧图片都放进最终分享包，只保留最终 Markdown 实际引用的图片。
- 如果一批视频画面布局一致，先交互式框选一次 PPT 区域，再复用保存的 sidecar 或显式 `--ppt-rect`。
