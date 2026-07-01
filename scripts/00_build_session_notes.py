#!/usr/bin/env python3
"""Run the local video-to-HTML article pipeline for one presentation video."""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def run(cmd: list[str], cwd: Path = SCRIPT_DIR) -> None:
    print("\n$ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def patch_pc_width_html(html_path: Path) -> None:
    text = html_path.read_text(encoding="utf-8")
    text = text.replace(
        """    body {
      margin: 0 auto;
      max-width: 36em;
      padding-left: 50px;
      padding-right: 50px;
      padding-top: 50px;
      padding-bottom: 50px;
      hyphens: auto;
      overflow-wrap: break-word;
      text-rendering: optimizeLegibility;
      font-kerning: normal;
    }""",
        """    body {
      margin: 0 auto;
      max-width: 1100px;
      padding: 48px 64px;
      hyphens: auto;
      overflow-wrap: break-word;
      text-rendering: optimizeLegibility;
      font-kerning: normal;
      line-height: 1.7;
    }""",
    )
    text = text.replace(
        """    img {
      max-width: 100%;
    }""",
        """    img {
      display: block;
      width: 100%;
      max-width: 100%;
      height: auto;
      margin: 0.75em auto;
    }""",
    )
    html_path.write_text(text, encoding="utf-8")


def remove_processor_prompts(markdown_path: Path) -> None:
    """Remove the prompt appendix added by 04_polish_slide_transcript.py."""
    text = markdown_path.read_text(encoding="utf-8")
    markers = [
        "\n\n## 处理过程中使用的提示（Prompts）",
        "\n## 处理过程中使用的提示（Prompts）",
    ]
    for marker in markers:
        idx = text.find(marker)
        if idx != -1:
            markdown_path.write_text(text[:idx].rstrip() + "\n", encoding="utf-8")
            return


def rewrite_md_links(markdown_path: Path, image_dir_name: str) -> None:
    text = markdown_path.read_text(encoding="utf-8")

    def replace_target(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_target = match.group(2).strip()
        target = raw_target[1:-1] if raw_target.startswith("<") and raw_target.endswith(">") else raw_target
        if target.startswith("ppt_pics/"):
            target = f"{image_dir_name}/{Path(target).name}"
        return f"![{alt}](<{target}>)"

    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []
    for line in lines:
        if line.lstrip().startswith("![") and "](" in line and any(ext in line.lower() for ext in IMAGE_EXTENSIONS):
            line = re.sub(r"!\[([^\]]*)\]\((<[^>]+>|[^)]+)\)", replace_target, line)
        new_lines.append(line)
    markdown_path.write_text("".join(new_lines), encoding="utf-8")


def referenced_image_names(markdown_path: Path, image_dir_name: str) -> set[str]:
    text = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(r"!\[[^\]]*\]\((<[^>]+>|[^)]+)\)")
    refs: set[str] = set()
    for raw in pattern.findall(text):
        ref = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        ref = unquote(ref)
        if ref.startswith(f"{image_dir_name}/"):
            refs.add(Path(ref).name)
    return refs


def remove_unreferenced_images(image_dir: Path, keep_names: set[str]) -> tuple[int, int]:
    kept = 0
    deleted = 0
    for image in image_dir.iterdir():
        if not image.is_file() or image.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if image.name in keep_names:
            kept += 1
        else:
            image.unlink()
            deleted += 1
    return kept, deleted


def verify_html_refs(share_dir: Path) -> tuple[int, list[tuple[str, str]]]:
    img_re = re.compile(r'<img[^>]+src="([^"]+)"')
    checked = 0
    missing: list[tuple[str, str]] = []
    for html in share_dir.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        for src in img_re.findall(text):
            if src.startswith(("http://", "https://", "data:")):
                continue
            checked += 1
            rel = unquote(src)
            if not (share_dir / rel).is_file():
                missing.append((html.name, src))
    return checked, missing


def embed_html_images(html_path: Path) -> tuple[int, list[str]]:
    """Inline local image refs so the HTML can be opened as a single file."""
    text = html_path.read_text(encoding="utf-8")
    base_dir = html_path.parent
    missing: list[str] = []
    embedded = 0

    def replace_src(match: re.Match[str]) -> str:
        nonlocal embedded
        quote = match.group(1)
        src = match.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return match.group(0)
        image_path = (base_dir / unquote(src)).resolve()
        if not image_path.is_file():
            missing.append(src)
            return match.group(0)
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        embedded += 1
        return f'src={quote}data:{mime_type};base64,{data}{quote}'

    text = re.sub(r'src=(["\'])([^"\']+\.(?:png|jpe?g|webp|gif))\1', replace_src, text, flags=re.I)
    html_path.write_text(text, encoding="utf-8")
    return embedded, missing


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a shareable HTML article from one video.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-root", type=Path, help="Default: <video_parent>/<video_stem>_html_output")
    parser.add_argument("--interactive", action="store_true", help="Open the PPT crop selector in step 02.")
    parser.add_argument("--ppt-rect", help="Manual PPT crop rect: x1,y1,x2,y2 in relative coordinates.")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument(
        "--detect-width",
        type=int,
        default=240,
        help="Low-res detection width passed to 02_extract_slide_timestamps.py.",
    )
    parser.add_argument(
        "--detection-backend",
        choices=["hybrid-keyframe", "accurate"],
        default="hybrid-keyframe",
        help="Slide detection backend passed to 02_extract_slide_timestamps.py.",
    )
    parser.add_argument(
        "--image-format",
        choices=["jpg", "jpeg", "png", "webp"],
        default="png",
        help="Slide image format passed to 02_extract_slide_timestamps.py.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=85,
        help="JPEG/WebP quality passed to 02_extract_slide_timestamps.py.",
    )
    parser.add_argument("--threads", type=int, default=4, help="Threads for 04_polish_slide_transcript segments.")
    parser.add_argument(
        "--polish-provider",
        choices=["openrouter", "codex-notes", "passthrough"],
        default="openrouter",
        help="openrouter keeps the original LLM flow; codex-notes reads Codex-authored notes; passthrough keeps aligned transcript text.",
    )
    parser.add_argument(
        "--codex-notes-dir",
        type=Path,
        help="Directory containing Codex notes for --polish-provider codex-notes.",
    )
    parser.add_argument("--compress-png", action="store_true", help="Compress referenced PNGs in the share folder.")
    parser.add_argument(
        "--embed-html-images",
        action="store_true",
        help="Inline local images into the generated HTML for robust single-file preview/sharing.",
    )
    parser.add_argument("--zip", action="store_true", help="Create a zip archive of the share folder.")
    args = parser.parse_args()

    video = args.video.resolve()
    if not video.is_file():
        raise SystemExit(f"Video not found: {video}")

    stem = video.stem
    output_root = (args.output_root or (video.parent / f"{stem}_html_output")).resolve()
    work_dir = output_root / "work"
    share_dir = output_root / "share"
    work_dir.mkdir(parents=True, exist_ok=True)
    if share_dir.exists():
        shutil.rmtree(share_dir)
    share_dir.mkdir(parents=True, exist_ok=True)

    transcript = video.with_name(f"{stem}_transcript.txt")
    if not transcript.exists():
        run([sys.executable, str(SCRIPT_DIR / "01_transcribe_video.py"), str(video), "--output", str(transcript)])
    else:
        print(f"Using existing transcript: {transcript}")

    slides_md = work_dir / f"{stem}.md"
    cmd02 = [
        sys.executable,
        str(SCRIPT_DIR / "02_extract_slide_timestamps.py"),
        str(video),
        "-o",
        str(work_dir),
        "-i",
        str(args.interval),
        "-t",
        str(args.threshold),
        "--md_name",
        slides_md.name,
        "--detect-width",
        str(args.detect_width),
        "--detection-backend",
        args.detection_backend,
        "--image-format",
        args.image_format,
        "--jpeg-quality",
        str(args.jpeg_quality),
    ]
    if args.interactive:
        cmd02.append("--interactive")
    if args.ppt_rect:
        cmd02.extend(["--ppt_rect", args.ppt_rect])
    run(cmd02)

    integrated_md = work_dir / f"{stem}_integrated.md"
    run(
        [
            sys.executable,
            str(SCRIPT_DIR / "03_integrate_transcript_slides.py"),
            str(slides_md),
            str(transcript),
            str(integrated_md),
        ]
    )

    processed_md = work_dir / f"{stem}_integrated_Processed.md"
    cmd04 = [
        sys.executable,
        str(SCRIPT_DIR / "04_polish_slide_transcript.py"),
        str(integrated_md),
        str(processed_md),
        "-y",
        "--no-review",
        "--mode",
        "light-plus",
        "--provider",
        args.polish_provider,
        "-t",
        str(args.threads),
    ]
    if args.codex_notes_dir:
        cmd04.extend(["--codex-notes-dir", str(args.codex_notes_dir.resolve())])
    run(cmd04)
    remove_processor_prompts(processed_md)

    final_md = share_dir / f"{stem}.md"
    image_dir_name = f"{stem}_ppt_pics"
    final_image_dir = share_dir / image_dir_name
    shutil.copy2(processed_md, final_md)
    shutil.copytree(work_dir / "ppt_pics", final_image_dir)
    rewrite_md_links(final_md, image_dir_name)

    keep = referenced_image_names(final_md, image_dir_name)
    kept, deleted = remove_unreferenced_images(final_image_dir, keep)
    print(f"Referenced images kept: {kept}; unreferenced images deleted: {deleted}")

    html_path = share_dir / f"{stem}.html"
    run(["pandoc", str(final_md), "-o", str(html_path), "--standalone", "--metadata", f"title={stem}"])
    patch_pc_width_html(html_path)

    if args.embed_html_images:
        embedded, missing_embed = embed_html_images(html_path)
        if missing_embed:
            print(f"Missing images while embedding: {len(missing_embed)}")
            for src in missing_embed[:10]:
                print(f"  {src}")
            raise SystemExit(1)
        print(f"Embedded HTML images: {embedded}")

    if args.compress_png:
        compressor = SCRIPT_DIR / "05_compress_png_images.py"
        if not compressor.exists():
            print("05_compress_png_images.py not found; skipping PNG compression.")
        else:
            run([sys.executable, str(compressor), "--share-dir", str(share_dir), "--threads", "12"])

    checked, missing = verify_html_refs(share_dir)
    if missing:
        print(f"Missing image references: {len(missing)}")
        for html_name, src in missing[:10]:
            print(f"  {html_name}: {src}")
        raise SystemExit(1)
    print(f"Verified HTML image references: {checked}")

    if args.zip:
        zip_path = output_root / f"{stem}_html_share.zip"
        if zip_path.exists():
            zip_path.unlink()
        run(["zip", "-0", "-qr", str(zip_path), share_dir.name], cwd=output_root)
        print(f"Zip created: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")

    print(f"HTML created: {html_path}")
    print(f"Share folder: {share_dir}")


if __name__ == "__main__":
    main()
