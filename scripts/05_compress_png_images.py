#!/usr/bin/env python3
"""
Compress PNG images in an HTML share folder while keeping PNG format.

Pipeline per image:
1. pngquant: lossy palette quantization, still outputs .png
2. oxipng: lossless PNG structure optimization

The script keeps filenames unchanged, so existing HTML references remain valid.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import subprocess
from pathlib import Path

from PIL import Image


DEFAULT_SHARE_DIR = Path("/Volumes/1TB/meetup_clean/Qcon 2026 视频/Qcon_2026_HTML_share")


def pillow_quantize_in_place(path: Path, colors: int) -> None:
    """Fallback compressor when pngquant is unavailable or fails."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with Image.open(path) as img:
        rgba = img.convert("RGBA")
        # Adaptive palette keeps slide text readable while reducing color count.
        quantized = rgba.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        quantized.save(tmp, "PNG", optimize=True)
    tmp.replace(path)


def compress_one(
    path: Path,
    pngquant_bin: str | None,
    oxipng_bin: str | None,
    quality: str,
    colors: int,
    oxipng_level: int,
) -> tuple[Path, int, int, str]:
    before = path.stat().st_size
    method = "none"

    if pngquant_bin:
        # --skip-if-larger avoids replacing files that cannot be compressed well.
        result = subprocess.run(
            [
                pngquant_bin,
                "--force",
                "--skip-if-larger",
                "--ext",
                ".png",
                "--quality",
                quality,
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # pngquant returns 98 when quality cannot be met; fall back to Pillow.
        if result.returncode == 0:
            method = "pngquant"
        else:
            pillow_quantize_in_place(path, colors=colors)
            method = "pillow"
    else:
        pillow_quantize_in_place(path, colors=colors)
        method = "pillow"

    if oxipng_bin:
        subprocess.run(
            [
                oxipng_bin,
                "-o",
                str(oxipng_level),
                "--strip",
                "safe",
                "--quiet",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    after = path.stat().st_size
    return path, before, after, method


def verify_png_refs(share_dir: Path) -> tuple[int, int]:
    import re
    from urllib.parse import unquote

    img_re = re.compile(r'<img[^>]+src="([^"]+)"')
    checked = 0
    missing = 0
    for html in sorted(share_dir.glob("*.html")):
        text = html.read_text(encoding="utf-8")
        for src in img_re.findall(text):
            if src.startswith(("http://", "https://", "data:")):
                continue
            checked += 1
            rel = unquote(src)
            if not rel.endswith(".png") or not (share_dir / rel).is_file():
                missing += 1
    return checked, missing


def main() -> None:
    parser = argparse.ArgumentParser(description="Compress share PNG images in place.")
    parser.add_argument("--share-dir", type=Path, default=DEFAULT_SHARE_DIR)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument(
        "--quality",
        default="45-75",
        help="pngquant quality range, e.g. 45-75. Lower means smaller PNGs.",
    )
    parser.add_argument("--colors", type=int, default=128, help="Pillow fallback palette size.")
    parser.add_argument("--oxipng-level", type=int, default=2)
    args = parser.parse_args()

    share_dir = args.share_dir
    if not share_dir.is_dir():
        raise SystemExit(f"Share directory not found: {share_dir}")

    pngs = sorted(share_dir.glob("*_ppt_pics/*.png"))
    if not pngs:
        raise SystemExit("No PNG files found.")

    pngquant_bin = shutil.which("pngquant")
    oxipng_bin = shutil.which("oxipng")
    before_total = sum(path.stat().st_size for path in pngs)

    print(f"png_count={len(pngs)}")
    print(f"before_mb={before_total / 1024 / 1024:.1f}")
    print(f"threads={args.threads}")
    print(f"pngquant={pngquant_bin or 'not found'}")
    print(f"oxipng={oxipng_bin or 'not found'}")
    print(f"quality={args.quality}")

    converted = 0
    methods: dict[str, int] = {}
    after_total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = [
            executor.submit(
                compress_one,
                path,
                pngquant_bin,
                oxipng_bin,
                args.quality,
                args.colors,
                args.oxipng_level,
            )
            for path in pngs
        ]
        for future in concurrent.futures.as_completed(futures):
            _path, _before, after, method = future.result()
            converted += 1
            after_total += after
            methods[method] = methods.get(method, 0) + 1
            if converted % 250 == 0 or converted == len(pngs):
                print(f"compressed={converted}/{len(pngs)}", flush=True)

    checked, missing = verify_png_refs(share_dir)
    print(f"compressed={converted}")
    print(f"after_mb={after_total / 1024 / 1024:.1f}")
    print(f"ratio={after_total / before_total:.3f}")
    print(f"methods={methods}")
    print(f"verified_refs={checked}")
    print(f"missing_or_non_png_refs={missing}")
    if missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
