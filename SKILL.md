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
- 只包含 HTML 和实际引用图片的分享目录
- 可选的 zip 压缩包

## 内置脚本

本 Skill 可单独分发。它在 `scripts/` 目录内置以下可执行脚本：

- `01_transcribe_video.py`
- `02_extract_slide_timestamps.py`
- `03_integrate_transcript_slides.py`
- `04_polish_slide_transcript.py`
- `05_prepare_codex_batches.py`
- `06_merge_light_polish_scenes.py`
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

然后只需填写：

- `VOLCENGINE_API_KEY`：火山引擎新版控制台的 API Key

默认 Codex sub-agent 路径不需要 OpenRouter Key。`API_KEY`、`BASE_URL` 和 `MODEL` 只为兼容旧的 OpenRouter provider 保留，均为可选配置。

转录脚本使用火山引擎录音文件极速版 HTTP 接口，把本地音频 Base64 编码后放入 `audio.data` 直接发送。无需对象存储、公网音频 URL，也无需 submit/query 轮询。旧版控制台鉴权可额外配置 `APP_KEY` 和 `ACCESS_KEY`。

极速版单文件限制为 2 小时、100MB，支持 WAV、MP3 和 OGG OPUS。超过限制时应先切分音频，或改用标准版 URL 提交接口。

不要分享或提交 `scripts/config.py`；它已经被 `scripts/.gitignore` 忽略。

内置脚本会从自己的 `scripts/` 目录导入 `config.py`，因此单独分发后的 Skill 只需要在本地补齐 `scripts/config.py`。

## 推荐命令

### 路径 A：Codex sub-agent（默认）

先使用 helper 脚本完成转写、截图和稿件对齐；默认 `passthrough` 不会调用 OpenRouter：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" \
  --interactive \
  --polish-provider passthrough
```

如果视频旁边已经有保存好的 PPT 截图区域 sidecar（`<video>.ppt_rect.json`），可以省略 `--interactive`：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" \
  --polish-provider passthrough
```

如果一批视频画面布局一致，可以复用已知截图区域：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" \
  --ppt-rect "0.0359,0.0806,0.6792,0.7296" \
  --polish-provider passthrough
```

然后使用 `05_prepare_codex_batches.py` 准备批次，让多个 sub-agent 并行生成 notes，再用 `--provider codex-notes` 汇总并运行 `06_merge_light_polish_scenes.py`。

`codex_notes` 中每页可以使用以下文件名之一：

- `slide_01.md`
- `slide_1.md`
- `<frame_stem>.md`，例如 `frame_00_01_20_000.md`

每个 note 推荐包含：

```markdown
## 幻灯片要点

- 这一页的标题、术语、图表关系、容易被 ASR 写错的词。

## 轻量打磨稿

这里放 Codex 结合图片和该页口述稿整理后的正文。
```

如果需要并行处理，先准备批次：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/05_prepare_codex_batches.py \
  /path/to/video_integrated.md \
  --output-dir /path/to/codex_batches \
  --notes-dir /path/to/codex_notes \
  --image-dir /path/to/ppt_pics \
  --batch-size 10
```

每个 sub-agent 只写自己负责页码的 `slide_N.md`。所有批次完成后，使用
`--provider codex-notes --codex-notes-dir /path/to/codex_notes` 汇总。

### 路径 B：OpenRouter（可选兼容）

只有明确需要旧 OpenRouter 自动路径时，才配置 `API_KEY` 并显式传入 `--polish-provider openrouter`。

调用方式：

```bash
python ~/.cursor/skills/session-notes-maker/scripts/00_build_session_notes.py "/path/to/video.mp4" \
  --ppt-rect "0.0359,0.0806,0.6792,0.7296" \
  --polish-provider openrouter \
  --zip
```

## 工作流程

1. **转录视频**
   - 如果视频旁边已有 `<video>_transcript.txt`，直接复用。
   - 否则运行 `01_transcribe_video.py`。
   - 脚本从视频抽取 16kHz 单声道 MP3，缓存后通过 `audio.data` 直传火山引擎 ASR Flash；响应在同一次 HTTP 请求中返回。

2. **生成幻灯片时间戳 Markdown**
   - 运行 `02_extract_slide_timestamps.py`。
   - 默认使用 `hybrid-keyframe`；动态布局、keyframe 召回不可用或显式传入 `--detection-backend accurate` 时，回退到 streaming accurate：顺序解码采样帧，在内存中做 SSIM，只写出变化帧。
   - 如需回到旧行为（先保存所有采样 PNG，再二次读取做 SSIM），传入 `--legacy_extract_all`。
   - 如果没有可靠的 PPT 截图区域，使用 `--interactive` 让用户框选。
   - 重复运行时，优先复用已保存的 `<video>.ppt_rect.json` 或显式传入 `--ppt-rect`。
   - `hybrid-keyframe` 先用 H.264 keyframes 快速召回候选，再只在候选附近做局部 2 秒网格 SSIM 精修。
   - 当 PPT 区域固定（显式 `--ppt-rect` 或 sidecar 已加载）时，默认会走 hybrid-keyframe；否则回退到准确扫描。
   - 默认检测宽度为 `--detect-width 240`，默认输出 `--image-format png`，优先保证 PPT 文字、线条和图表清晰度。
   - 完整 39:10 样本中，accurate 路径用时 `34.79s`，默认 hybrid-keyframe + PNG 用时 `9.34s`，同样输出 `28` 个 slide 起点，速度约为 `3.72x`；如遇到 keyframe 召回不稳定的视频，可显式传入 `--detection-backend accurate`。
   - 如果用户更看重速度或体积，可显式传入 `--image-format jpg --jpeg-quality 90`。

3. **整合转录稿**
   - 运行 `03_integrate_transcript_slides.py`。
   - 输入：第 2 步生成的 `<video>.md` 和 `<video>_transcript.txt`。
   - 输出：`<video>_integrated.md`。

4. **使用轻量打磨模式处理**
   - 默认先用 `passthrough` 保留对齐稿，再由多个 Codex sub-agent 并行生成 notes，最后用 `--polish-provider codex-notes` 汇总。
   - 这条默认路径不依赖 OpenRouter Key；`--polish-provider openrouter` 仅作为可选兼容路径保留。
   - 运行 `04_polish_slide_transcript.py`，常用参数：
     - `--mode light-plus`
     - `--provider openrouter|codex-notes|passthrough`
     - `--codex-notes-dir /path/to/codex_notes`
     - `--no-review`
     - `-y`
   - 输出：`<video>_integrated_Processed.md`。
   - 流水线会自动删除文件末尾的“处理过程中使用的提示（Prompts）”附录，最终 HTML 不包含提示词内容。

5. **合并每页 PPT 内的完整发言**
   - `light-plus` 的最终阅读稿不能保留逐条 ASR 时间戳。
   - 先由第 4 步逐页结合截图完成术语校正，再运行 `06_merge_light_polish_scenes.py`。
   - 严格保持一页 PPT 对应一段讲稿；只合并这一页内部的字幕碎片，不跨 PPT 合并。
   - 只在 PPT 标题保留该页整体时间范围，正文不再显示逐句时间戳。

```bash
python ~/.cursor/skills/session-notes-maker/scripts/06_merge_light_polish_scenes.py \
  /path/to/video_integrated_Processed.md \
  /path/to/video_light_polished.md \
  --paragraph-target 240
```

6. **构建可分享 HTML**
   - 把处理后的 Markdown 复制到分享目录，并命名为 `<video>.md`。
   - 把 `ppt_pics/` 复制为 `<video>_ppt_pics/`。
   - 将 Markdown 图片链接改写为 `<video>_ppt_pics/...`。
   - 只保留最终 Markdown 实际引用的图片（PNG/JPG/JPEG/WebP）。
   - 使用 `pandoc` 转换为 standalone HTML。
   - 将 HTML 正文宽度调整为适合 PC 阅读的宽度（`max-width: 1100px`），并让图片自适应正文宽度。
   - 如果本地浏览器或分享环境对相对图片路径不稳定，加入 `--embed-html-images`，把最终 HTML 中引用的本地图片内嵌为 data URI，打开单个 HTML 也能稳定显示。

7. **可选打包**
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
- 所有 `<img src="...">` 引用都能在分享目录内找到，或已被 `--embed-html-images` 内嵌为 `data:image/...`；
- 如果用户要求 PNG 格式，确认图片文件仍然是 PNG；
- 如果用户要求打包，报告最终 zip 路径和大小。

## 变更日志

- `2026-07-01`: 默认抽帧路径切换为 `hybrid-keyframe`，结合 keyframe 快速召回和局部 accurate 精修。
- `2026-07-01`: 默认截图格式保持 PNG，以保证 PPT 文字和线条质量；保留 `--image-format jpg|webp` 供小体积/极速场景使用。
- `2026-07-01`: 保留 `--detection-backend accurate` 作为保守回退路径。

## 实践经验

- 转录稿驱动的文章固定使用 `light-plus`，因为它以转录稿为主，只用幻灯片辅助校对和轻度润色。
- `light-plus` 最终稿应严格保持一页 PPT 对应一段完整讲稿；逐条时间戳只属于中间校对产物，不应直接进入交付稿。
- 不要把所有截帧图片都放进最终分享包，只保留最终 Markdown 实际引用的图片。
- 如果一批视频画面布局一致，先交互式框选一次 PPT 区域，再复用保存的 sidecar 或显式 `--ppt-rect`。
