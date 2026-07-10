#!/usr/bin/env python3
"""Prepare disjoint slide batches for parallel Codex light-plus polishing.

The generated batch files are input packets for Codex sub-agents. Each agent
should write one ``slide_N.md`` note per assigned slide into ``--notes-dir``.
The existing ``04_polish_slide_transcript.py --provider codex-notes`` then
merges those notes without making another model call.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


SLIDE_RE = re.compile(r"^##\s+Slide\s+(\d+)\b.*$", re.MULTILINE)


def split_slides(text: str) -> list[tuple[int, str]]:
    matches = list(SLIDE_RE.finditer(text))
    slides: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        slides.append((int(match.group(1)), text[match.start():end].strip()))
    return slides


def batch_text(slides: list[tuple[int, str]], image_dir: Path) -> str:
    parts = [
        "# Codex light-plus batch",
        "",
        "For each slide, inspect the referenced image and transcript. Write one note named `slide_N.md`.",
        "Keep the transcript as the primary source. Correct obvious ASR errors using the image, preserve meaning, and avoid inventing content.",
        "Each note should contain `## 幻灯片要点` and `## 轻量打磨稿`.",
        "",
    ]
    for number, section in slides:
        image_match = re.search(r"\(([^)]+\.(?:png|jpg|jpeg|webp))\)", section, re.IGNORECASE)
        image = image_match.group(1) if image_match else ""
        image_path = (image_dir / Path(image).name).resolve() if image else ""
        parts.extend([
            f"## Slide {number}",
            "",
            f"Image: `{image_path}`",
            "",
            section,
            "",
            "---",
            "",
        ])
    return "\n".join(parts).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("integrated", type=Path, help="Integrated Markdown with slide sections")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for batch input files")
    parser.add_argument("--notes-dir", type=Path, required=True, help="Directory where agents will write slide_N.md")
    parser.add_argument("--image-dir", type=Path, required=True, help="Directory containing slide images")
    parser.add_argument("--batch-size", type=int, default=10, help="Slides per agent batch")
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    text = args.integrated.read_text(encoding="utf-8")
    slides = split_slides(text)
    if not slides:
        raise SystemExit("No ## Slide sections found")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.notes_dir.mkdir(parents=True, exist_ok=True)
    for old in args.output_dir.glob("batch_*.md"):
        old.unlink()

    for offset in range(0, len(slides), args.batch_size):
        batch = slides[offset:offset + args.batch_size]
        batch_no = offset // args.batch_size + 1
        path = args.output_dir / f"batch_{batch_no:02d}.md"
        path.write_text(batch_text(batch, args.image_dir), encoding="utf-8")

    manifest = args.output_dir / "MANIFEST.txt"
    manifest.write_text(
        f"slides={len(slides)}\n"
        f"batches={(len(slides) + args.batch_size - 1) // args.batch_size}\n"
        f"batch_size={args.batch_size}\n"
        f"notes_dir={args.notes_dir.resolve()}\n",
        encoding="utf-8",
    )
    print(f"slides={len(slides)}")
    print(f"batches={(len(slides) + args.batch_size - 1) // args.batch_size}")
    print(f"output_dir={args.output_dir.resolve()}")
    print(f"notes_dir={args.notes_dir.resolve()}")


if __name__ == "__main__":
    main()
