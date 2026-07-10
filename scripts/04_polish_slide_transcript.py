#!/usr/bin/env python3
"""
演示文稿转录Markdown处理器

本脚本用于处理包含演示文稿转录内容的Markdown文件，功能包括：
1. 识别幻灯片-转录片段（图片+文字）
2. 为用户审核生成临时Markdown文件
3. 用大语言模型处理每个片段：
   - 描述幻灯片图片
   - 将图片描述与转录文本整合
4. 输出处理完成的最终Markdown文件

用法示例：
    python 04_polish_slide_transcript.py input.md [output.md] [-y] [-t 线程数] [-w] [--toc] [--reference=template.docx]
"""

import os
import sys
import argparse
import subprocess
import logging
import time
import concurrent.futures
from threading import Lock
import shutil
import re

import config
import markdown_llm_utils as utils

def open_file_in_editor(file_path: str) -> None:
    """
    Open a file in the default text editor.

    Args:
        file_path: Path to the file to open
    """
    if sys.platform == 'win32':
        os.startfile(file_path)
    elif sys.platform == 'darwin':  # macOS
        subprocess.run(['open', file_path])
    else:  # Linux and other Unix-like
        subprocess.run(['xdg-open', file_path])

def convert_md_to_docx(md_path: str, toc: bool = False, reference_doc: str = None, output_path: str = None) -> str:
    """
    Convert a Markdown file to a Word document using pandoc.

    Args:
        md_path: Path to the Markdown file
        toc: Whether to include a table of contents
        reference_doc: Path to a reference Word document for styling

    Returns:
        Path to the generated Word document
    """
    # Check if pandoc is installed
    if shutil.which('pandoc') is None:
        print("Error: pandoc is not installed or not in PATH.")
        print("Please install pandoc: https://pandoc.org/installing.html")
        return None

    # Generate output path
    docx_path = os.path.splitext(md_path)[0] + '.docx'

    # Build pandoc command
    cmd = ["pandoc", md_path, "-o", docx_path]

    # Add options
    if toc:
        cmd.append("--toc")

    if reference_doc and os.path.isfile(reference_doc):
        cmd.extend(["--reference-doc", reference_doc])

    # Run pandoc
    print(f"\nConverting {md_path} to {docx_path}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"Conversion successful! Word document saved to: {docx_path}")
        return docx_path
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e.stderr}")
        return None

def _get_mode_prompts(mode: str) -> tuple[str, str]:
    """Return slide/image and transcript integration prompts for the selected mode."""
    if mode == "light-plus":
        return (
            config.LIGHT_DESCRIBE_SLIDE_PROMPT,
            config.LIGHT_TRANSCRIPT_INTEGRATION_PROMPT,
        )
    return (
        config.DESCRIBE_SLIDE_PROMPT,
        config.TRANSCRIPT_INTEGRATION_PROMPT,
    )


def _strip_speaker_labels(text: str) -> str:
    """Light cleanup that is safe without an LLM."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*说话人\d+\s*[:：]\s*", "", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _section_after_heading(text: str, heading_patterns: list[str]) -> str:
    """Extract a Markdown section by heading name, returning empty string if absent."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        normalized = line.strip().lower()
        if any(re.match(pattern, normalized, re.IGNORECASE) for pattern in heading_patterns):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^\s{0,3}#{1,6}\s+", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _find_codex_note(notes_dir: str, idx: int, image_path: str) -> str:
    """Find a Codex-authored note for one slide segment."""
    if not notes_dir:
        return ""
    image_stem = os.path.splitext(os.path.basename(image_path))[0]
    candidates = [
        f"slide_{idx + 1:02d}.md",
        f"slide_{idx + 1}.md",
        f"{idx + 1:02d}_{image_stem}.md",
        f"{idx + 1}_{image_stem}.md",
        f"{image_stem}.md",
    ]
    for name in candidates:
        path = os.path.join(notes_dir, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    return ""


def _apply_codex_note(idx: int, segment: dict, image_path: str, notes_dir: str) -> dict:
    """Use Codex-authored notes to build a processed segment without OpenRouter."""
    note = _find_codex_note(notes_dir, idx, image_path)
    cleaned_transcript = _strip_speaker_labels(segment["text"])
    if not note:
        return {
            "header": segment.get("header"),
            "images": segment["images"],
            "text": cleaned_transcript,
        }

    polished = _section_after_heading(
        note,
        [
            r"^#+\s*(lightly\s+polished\s+transcript|polished\s+transcript|final\s+text)\s*$",
            r"^#+\s*(轻量打磨稿|整理稿|最终稿|正文)\s*$",
        ],
    )
    if polished:
        text = polished
    else:
        slide_notes = _section_after_heading(
            note,
            [
                r"^#+\s*(slide\s+context|image\s+notes|slide\s+notes)\s*$",
                r"^#+\s*(幻灯片要点|图片要点|校对要点)\s*$",
            ],
        ) or note
        text = f"**幻灯片校对要点**\n\n{slide_notes}\n\n**口述稿**\n\n{cleaned_transcript}"

    return {
        "header": segment.get("header"),
        "images": segment["images"],
        "text": text.strip(),
    }


def process_markdown(
    input_path: str,
    output_path: str,
    yes_to_all: bool = False,
    num_threads: int = 5,
    mode: str = "rewrite",
    no_review: bool = False,
    provider: str = "openrouter",
    codex_notes_dir: str = "",
) -> None:
    """
    Process a Markdown file containing presentation transcript.

    Args:
        input_path: Path to the input Markdown file
        output_path: Path to save the processed Markdown file
        yes_to_all: Automatically confirm all API calls
        num_threads: Number of threads for parallel processing
        mode: Processing mode. "rewrite" keeps the original rewrite-heavy flow;
              "light-plus" keeps transcript as primary source and uses slides mainly for correction.
    """
    # Set up logging
    utils.setup_logging(
        config.LOG_FILE,
        config.LOG_LEVEL,
        config.LOG_FORMAT
    )

    # Read the input file
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading input file: {e}")
        return

    # Parse the markdown content
    segments = utils.parse_markdown(content)

    # Create a temporary markdown file in the same directory as the input file
    input_dir = os.path.dirname(os.path.abspath(input_path))
    input_filename = os.path.basename(input_path)
    temp_filename = f"{os.path.splitext(input_filename)[0]}_temp.md"
    temp_path = os.path.join(input_dir, temp_filename)

    utils.create_temp_markdown(segments, temp_path)

    if no_review:
        print(f"--no-review set; skipping manual review. Temp file at: {temp_path}")
    else:
        print(f"Opening temporary file for review: {temp_path}")
        print("Please review the segmentation, make any necessary adjustments, then save and close the file.")
        try:
            open_file_in_editor(temp_path)
            input("Press Enter when you have finished reviewing and saved the temporary file...")
        except EOFError:
            print("No interactive input available; skipping manual review step and using auto-generated segments.")

    # Read the updated segments
    updated_segments = utils.read_temp_markdown(temp_path)
    print(f"Found {len(updated_segments)} segments in the updated file.")

    if provider == "codex-notes" and not codex_notes_dir:
        raise SystemExit("--provider codex-notes requires --codex-notes-dir")

    # Set up OpenAI client for OpenRouter only on the original path.
    client = None
    if provider == "openrouter":
        client = utils.get_client(
            config.API_KEY,
            config.BASE_URL,
            config.SITE_URL,
            config.SITE_NAME
        )

    describe_prompt, integration_prompt_template = _get_mode_prompts(mode)

    # Set up thread-safe variables
    processed_segments = [None] * len(updated_segments)
    progress_lock = Lock()
    progress_counter = 0

    def process_segment(idx, segment):
        nonlocal progress_counter

        # Thread-safe progress update
        with progress_lock:
            progress_counter += 1
            current_progress = progress_counter
            print(f"\nProcessing segment {idx+1}/{len(updated_segments)} (Thread {current_progress}/{len(updated_segments)})...")

        # Special handling for segments without images (like document title)
        if not segment['images']:
            with progress_lock:
                print(f"Segment {idx+1}: No images in this segment (likely document title or introduction), preserving as is...")
            return segment

        # Get the first image path
        image_markdown = segment['images'][0]
        image_path = utils.extract_image_path(image_markdown, input_dir)

        # Check if the image exists
        if not os.path.exists(image_path):
            with progress_lock:
                print(f"Segment {idx+1}: Image not found: {image_path}")
            return segment

        if provider == "passthrough":
            with progress_lock:
                print(f"Segment {idx+1}: passthrough provider; keeping aligned transcript text.")
            return {
                "header": segment.get("header"),
                "images": segment["images"],
                "text": _strip_speaker_labels(segment["text"]),
            }

        if provider == "codex-notes":
            with progress_lock:
                print(f"Segment {idx+1}: using Codex notes from {codex_notes_dir}")
            return _apply_codex_note(idx, segment, image_path, codex_notes_dir)

        # Step 1: Describe the slide
        with progress_lock:
            print(f"\nSegment {idx+1}: Step 1: Describing the slide...")
            print(f"Mode: {mode}")
            print(f"Using prompt: {describe_prompt}")

        slide_description = utils.call_llm_with_image(
            client,
            config.MODEL,
            describe_prompt,
            image_path,
            yes_to_all=yes_to_all
        )

        if slide_description.startswith("Error") or slide_description.startswith("API call cancelled"):
            with progress_lock:
                print(f"Segment {idx+1}: Skipping integration due to error in slide description: {slide_description}")
            return segment

        with progress_lock:
            print(f"\nSegment {idx+1}: Slide description:")
            print(slide_description)

        # Step 2: Integrate with transcript
        with progress_lock:
            print(f"\nSegment {idx+1}: Step 2: Integrating with transcript...")

        integration_prompt = integration_prompt_template.format(
            slide_description=slide_description,
            transcript_text=segment['text']
        )

        integrated_text = utils.call_llm_with_text(
            client,
            config.MODEL,
            integration_prompt,
            yes_to_all=yes_to_all
        )

        if integrated_text.startswith("Error") or integrated_text.startswith("API call cancelled"):
            with progress_lock:
                print(f"Segment {idx+1}: Using original text due to error in integration: {integrated_text}")
            return segment

        with progress_lock:
            print(f"\nSegment {idx+1}: Integrated text:")
            print(integrated_text)

        # Create processed segment
        return {
            'header': segment.get('header'),
            'images': segment['images'],
            'text': integrated_text
        }

    # Process segments in parallel
    print(f"\nProcessing {len(updated_segments)} segments using {num_threads} threads...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        # Submit all tasks
        future_to_idx = {executor.submit(process_segment, i, segment): i for i, segment in enumerate(updated_segments)}

        # Process results as they complete
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                processed_segments[idx] = future.result()
            except Exception as e:
                print(f"Error processing segment {idx+1}: {e}")
                processed_segments[idx] = updated_segments[idx]  # Use original segment on error

    # Create the final markdown file
    with open(output_path, 'w', encoding='utf-8') as f:
        for segment in processed_segments:
            if segment.get('header'):
                f.write(f"{segment['header']}\n\n")

            # Write images
            for image in segment['images']:
                f.write(f"{image}\n\n")

            # Write text
            f.write(segment['text'].strip() + "\n\n")

            # Add separator
            f.write("---\n\n")

        # Append prompts from md_config.py
        f.write("\n\n## 处理过程中使用的提示（Prompts）\n\n")
        f.write(f"### 处理模式\n\n`{mode}`\n\n")
        f.write("### 幻灯片描述提示\n\n```\n")
        f.write(describe_prompt)
        f.write("\n```\n\n")
        f.write("### 文本整合提示\n\n```\n")
        f.write(integration_prompt_template)
        f.write("\n```\n")

    print(f"\nProcessing complete! Output saved to: {output_path}")

    # Display total cost
    print(f"\nEstimated total cost: ${utils.total_cost:.6f}")
    logging.info(f"Estimated total cost: ${utils.total_cost:.6f}")

    if provider == "openrouter":
        # Get actual usage from OpenRouter
        print("\nGetting actual usage from OpenRouter...")
        usage_data = utils.get_openrouter_usage(config.API_KEY)
        if 'error' not in usage_data:
            if 'data' in usage_data:
                # 新版API响应格式
                if 'usage' in usage_data['data']:
                    spend = usage_data['data']['usage']
                    print(f"Actual spend according to OpenRouter: ${spend:.6f}")
                    logging.info(f"Actual spend according to OpenRouter: ${spend:.6f}")
                # 旧版API响应格式
                elif 'key' in usage_data['data'] and 'spend' in usage_data['data']['key']:
                    spend = usage_data['data']['key']['spend']
                    print(f"Actual spend according to OpenRouter: ${spend:.6f}")
                    logging.info(f"Actual spend according to OpenRouter: ${spend:.6f}")
                else:
                    print("Spend information not available from OpenRouter.")
                    logging.info("Spend information not available from OpenRouter.")
            else:
                print("Unexpected response format from OpenRouter.")
                logging.info(f"Unexpected response format from OpenRouter: {usage_data}")
        else:
            print(f"Error getting usage from OpenRouter: {usage_data['error']}")
            logging.info(f"Error getting usage from OpenRouter: {usage_data['error']}")

    # Keep the temporary file
    print(f"\nTemporary file kept at: {temp_path}")
    logging.info(f"Temporary file kept at: {temp_path}")

def main():
    """Main function to parse arguments and run the processor."""
    parser = argparse.ArgumentParser(description='Process Markdown files containing presentation transcripts.')
    parser.add_argument('input', help='Input Markdown file')
    parser.add_argument('output', nargs='?', help='Output Markdown file (optional)')
    parser.add_argument('-y', '--yes', action='store_true', help='Automatically confirm all API calls')
    parser.add_argument('-t', '--threads', type=int, default=5, help='Number of threads for parallel processing (default: 5)')
    parser.add_argument(
        '--mode',
        choices=['rewrite', 'light-plus'],
        default='rewrite',
        help='Processing mode: rewrite keeps the original behavior; light-plus uses slides mainly to correct and lightly refine the transcript.'
    )
    parser.add_argument(
        '--provider',
        choices=['openrouter', 'codex-notes', 'passthrough'],
        default='openrouter',
        help='openrouter keeps the original API path; codex-notes reads Codex-authored notes; passthrough only cleans speaker labels.'
    )
    parser.add_argument(
        '--codex-notes-dir',
        help='Directory of Codex-authored slide notes for --provider codex-notes.'
    )
    parser.add_argument('--no-review', action='store_true', help='Skip the manual segment-review step (for batch / non-interactive runs).')
    parser.add_argument('-w', '--word', action='store_true', help='Convert the output Markdown to Word document')
    parser.add_argument('--toc', action='store_true', help='Include table of contents in Word document (only with --word)')
    parser.add_argument('--reference', help='Reference Word document for styling (only with --word)')

    args = parser.parse_args()

    # Check if input file exists
    if not os.path.isfile(args.input):
        print(f"Error: Input file '{args.input}' not found.")
        sys.exit(1)

    # Generate default output filename if not provided
    output_path = args.output
    if not output_path:
        input_base = os.path.basename(args.input)
        input_name, input_ext = os.path.splitext(input_base)
        # Format: YYYYmmdd_hhmmss (actual time in lowercase)
        timestamp = time.strftime("%Y%m%d_%H%M%S").lower()
        output_filename = f"{input_name}_Processed_{timestamp}.md"
        output_path = os.path.join(os.path.dirname(os.path.abspath(args.input)), output_filename)
        print(f"No output file specified. Using default: {output_path}")

    # Process the markdown file
    process_markdown(
        args.input,
        output_path,
        yes_to_all=args.yes,
        num_threads=args.threads,
        mode=args.mode,
        no_review=args.no_review,
        provider=args.provider,
        codex_notes_dir=args.codex_notes_dir or "",
    )

    # Convert to Word if requested
    if args.word:
        convert_md_to_docx(output_path, toc=args.toc, reference_doc=args.reference)

if __name__ == "__main__":
    main()
