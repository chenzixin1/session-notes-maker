#!/usr/bin/env python3
"""Run the local video-to-HTML article pipeline for one presentation video."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent


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
    text = text.replace("(ppt_pics/", f"(<{image_dir_name}/")

    # If the source already had unwrapped rewritten links, make sure image links
    # are angle-wrapped so spaces and parentheses in folder names are safe.
    lines = text.splitlines(keepends=True)
    new_lines: list[str] = []
    for line in lines:
        if line.lstrip().startswith("![") and "](" in line and ".png" in line:
            start = line.find("](")
            end = line.rfind(")")
            if start != -1 and end != -1:
                target = line[start + 2 : end]
                if target.endswith(".png") and not (target.startswith("<") and target.endswith(">")):
                    line = line[: start + 2] + "<" + target + ">" + line[end:]
        new_lines.append(line)
    markdown_path.write_text("".join(new_lines), encoding="utf-8")


def referenced_png_names(markdown_path: Path, image_dir_name: str) -> set[str]:
    text = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(r"!\[[^\]]*\]\((<[^>]+>|[^)]+)\)")
    refs: set[str] = set()
    for raw in pattern.findall(text):
        ref = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        ref = unquote(ref)
        if ref.startswith(f"{image_dir_name}/"):
            refs.add(Path(ref).name)
    return refs


def remove_unreferenced_pngs(image_dir: Path, keep_names: set[str]) -> tuple[int, int]:
    kept = 0
    deleted = 0
    for png in image_dir.glob("*.png"):
        if png.name in keep_names:
            kept += 1
        else:
            png.unlink()
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a shareable HTML article from one video.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-root", type=Path, help="Default: <video_parent>/<video_stem>_html_output")
    parser.add_argument("--interactive", action="store_true", help="Open the PPT crop selector in step 02.")
    parser.add_argument("--ppt-rect", help="Manual PPT crop rect: x1,y1,x2,y2 in relative coordinates.")
    parser.add_argument(
        "--full-frame",
        action="store_true",
        help="Step 02: use entire video frame for screenshots and SSIM (no region crop or mixed-layout detection).",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument(
        "--output-max-side",
        type=int,
        default=0,
        help="Step 02 streaming mode: resize saved screenshots to this longest side; 0 keeps original resolution.",
    )
    parser.add_argument(
        "--legacy-extract-all",
        action="store_true",
        help="Step 02: save every sampled PNG before SSIM comparison instead of the faster streaming mode.",
    )
    parser.add_argument("--threads", type=int, default=4, help="Threads for 04_polish_slide_transcript segments.")
    parser.add_argument("--compress-png", action="store_true", help="Compress referenced PNGs in the share folder.")
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
        "--output_max_side",
        str(args.output_max_side),
    ]
    if args.interactive:
        cmd02.append("--interactive")
    if args.ppt_rect:
        cmd02.extend(["--ppt_rect", args.ppt_rect])
    if args.full_frame:
        cmd02.append("--full-frame")
    if args.legacy_extract_all:
        cmd02.append("--legacy_extract_all")
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
    run(
        [
            sys.executable,
            str(SCRIPT_DIR / "04_polish_slide_transcript.py"),
            str(integrated_md),
            str(processed_md),
            "-y",
            "--no-review",
            "--mode",
            "light-plus",
            "-t",
            str(args.threads),
        ]
    )
    remove_processor_prompts(processed_md)

    final_md = share_dir / f"{stem}.md"
    image_dir_name = f"{stem}_ppt_pics"
    final_image_dir = share_dir / image_dir_name
    shutil.copy2(processed_md, final_md)
    shutil.copytree(work_dir / "ppt_pics", final_image_dir)
    rewrite_md_links(final_md, image_dir_name)

    keep = referenced_png_names(final_md, image_dir_name)
    kept, deleted = remove_unreferenced_pngs(final_image_dir, keep)
    print(f"Referenced PNGs kept: {kept}; unreferenced PNGs deleted: {deleted}")

    html_path = share_dir / f"{stem}.html"
    run(["pandoc", str(final_md), "-o", str(html_path), "--standalone", "--metadata", f"title={stem}"])
    patch_pc_width_html(html_path)

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
