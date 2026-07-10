#!/usr/bin/env python3
"""Merge timestamped cues into one light-polished speech per PPT page.

The upstream Codex/OpenRouter pass is deliberately slide-oriented so each image
can correct its own transcript. This postprocessor keeps that one-to-one PPT
mapping, removes cue timestamps, and joins each page's corrected utterances into
natural paragraphs.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


SLIDE_RE = re.compile(
    r"^##\s+Slide\s+(\d+)\s+\(Timestamp:\s*([^)]+)\)\s*$",
    re.MULTILINE,
)
IMAGE_RE = re.compile(r"!\[[^]]*\]\(([^)]+)\)")
CUE_RE = re.compile(
    r"\*\*`\[([^]]+)\]`\*\*\s*\n(.*?)(?=\n\*\*`\[|\n---|\Z)",
    re.DOTALL,
)
SENTENCE_RE = re.compile(r".*?[。！？!?；](?=\s*|$)|.+$", re.DOTALL)
FILLER_RE = re.compile(r"^[\s，,。.!！?？]*(?:嗯+|啊+|呃+|好+|好的|对吧)[\s，,。.!！?？]*$")


@dataclass
class Slide:
    number: int
    timestamp: str
    image: str | None
    cues: list[tuple[str, str]]


def parse_slides(markdown: str) -> tuple[str, list[Slide]]:
    matches = list(SLIDE_RE.finditer(markdown))
    if not matches:
        raise ValueError("No slide sections found")
    preamble = markdown[:matches[0].start()].rstrip()
    slides: list[Slide] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end():end]
        image_match = IMAGE_RE.search(body)
        cues = []
        for stamp, text in CUE_RE.findall(body):
            cleaned = re.sub(r"\s+", " ", text).strip()
            if cleaned and not FILLER_RE.match(cleaned):
                cues.append((stamp.strip(), cleaned))
        slides.append(
            Slide(
                number=int(match.group(1)),
                timestamp=match.group(2).strip(),
                image=image_match.group(1) if image_match else None,
                cues=cues,
            )
        )
    return preamble, slides


def join_utterances(values: list[str]) -> str:
    result = ""
    for value in values:
        value = value.strip()
        if not value:
            continue
        if result and result[-1].isascii() and result[-1].isalnum() and value[0].isascii() and value[0].isalnum():
            result += " "
        result += value
    result = re.sub(r"([，。！？；：])\1+", r"\1", result)
    result = re.sub(r"\s+([，。！？；：])", r"\1", result)
    result = re.sub(r"([，。！？；：])\s+", r"\1", result)
    if result and result[-1] in "，,、：:":
        result = result[:-1] + "。"
    elif result and result[-1] not in "。！？!?；":
        result += "。"
    return result


def split_long_sentence(sentence: str, target: int) -> list[str]:
    if len(sentence) <= target * 2:
        return [sentence]
    chunks: list[str] = []
    rest = sentence
    while len(rest) > target * 2:
        candidates = [m.end() for m in re.finditer(r"[，；：]", rest[: target * 2])]
        cut = min(candidates, key=lambda pos: abs(pos - target)) if candidates else target
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def paragraphize(text: str, target: int) -> list[str]:
    sentences: list[str] = []
    for match in SENTENCE_RE.finditer(text):
        sentence = match.group(0).strip()
        if sentence:
            sentences.extend(split_long_sentence(sentence, target))

    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target * 1.45:
            paragraphs.append(current.strip())
            current = sentence
        else:
            current += sentence
        if len(current) >= target and current[-1] in "。！？!?；":
            paragraphs.append(current.strip())
            current = ""
    if current.strip():
        paragraphs.append(current.strip())
    return paragraphs


def short_time(value: str) -> str:
    value = value.strip()
    if "." in value:
        value = value.split(".", 1)[0]
    return value


def scene_range(slides: list[Slide]) -> tuple[str, str]:
    stamps = [stamp for slide in slides for stamp, _ in slide.cues]
    if stamps:
        first = stamps[0].split(" - ", 1)[0]
        last = stamps[-1].split(" - ", 1)[-1]
        return short_time(first), short_time(last)
    return short_time(slides[0].timestamp), short_time(slides[-1].timestamp)


def build_markdown(
    preamble: str,
    slides: list[Slide],
    paragraph_target: int,
) -> str:
    preamble = preamble.replace(
        "演讲幻灯片、完整中文字幕转录及问答环节",
        "演讲幻灯片、逐页对应的轻度打磨讲稿及问答环节",
    ).replace(
        "时间戳对应原视频位置。",
        "PPT 标题中的时间范围对应原视频位置，每页正文已合并为完整讲稿。",
    )
    parts = [preamble]
    if preamble:
        parts.append("")
    for slide in slides:
        start, end = scene_range([slide])
        parts.extend([
            f"## PPT {slide.number}（{start}-{end}）",
            "",
        ])
        if slide.image:
            parts.extend([
                f"![PPT {slide.number}]({slide.image})",
                "",
            ])
        speech = join_utterances([text for _, text in slide.cues])
        paragraphs = paragraphize(speech, paragraph_target) if speech else []
        if paragraphs:
            for paragraph in paragraphs:
                parts.extend([paragraph, ""])
        else:
            parts.extend(["本场景没有可用的口述内容。", ""])
        parts.extend(["---", ""])
    return "\n".join(parts).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Timestamped, slide-polished Markdown")
    parser.add_argument("output", type=Path, help="One-PPT-to-one-speech Markdown")
    parser.add_argument("--paragraph-target", type=int, default=240)
    args = parser.parse_args()
    if args.paragraph_target < 80:
        parser.error("--paragraph-target must be at least 80")

    preamble, slides = parse_slides(args.input.read_text(encoding="utf-8"))
    output = build_markdown(preamble, slides, args.paragraph_target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(f"slides={len(slides)}")
    print(f"sections={len(slides)}")
    print(f"output={args.output.resolve()}")


if __name__ == "__main__":
    main()
