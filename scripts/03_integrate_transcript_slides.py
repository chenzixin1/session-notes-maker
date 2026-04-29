#!/usr/bin/env python3
"""
集成幻灯片时间戳Markdown与带时间戳的转录文本，输出包含整合后内容的Markdown文件。

该脚本支持以下功能：
1. 从幻灯片Markdown中提取幻灯片和时间戳信息
2. 从转录文本中提取带时间戳的文本片段
3. 根据时间戳匹配幻灯片与对应的转录文本
4. 输出一个整合后的Markdown文件，其中包含幻灯片图片和对应的时间戳转录文本

Usage:
    python 03_integrate_transcript_slides.py slides.md transcript.txt [output.md]

Example:
    python 03_integrate_transcript_slides.py hornor_20250501174425.md data/inputs/hornor/transcripts/hornor_transcript.txt hornor_integrated.md
"""

import re
import argparse
import os
import sys
from datetime import datetime, timedelta, time

def timestamp_to_seconds(ts_str: str) -> float:
    """Converts HH:MM:SS or HH:MM:SS.mmm timestamp string to seconds."""
    try:
        # Try parsing with milliseconds
        dt_obj = datetime.strptime(ts_str, '%H:%M:%S.%f')
    except ValueError:
        try:
            # Try parsing without milliseconds
            dt_obj = datetime.strptime(ts_str, '%H:%M:%S')
        except ValueError:
            print(f"Error: Invalid timestamp format '{ts_str}'. Expected HH:MM:SS or HH:MM:SS.mmm")
            # Return a value that won't interfere? Or raise error?
            # Let's return 0.0 for now, assuming it might be a header or similar.
            return 0.0
    # Calculate total seconds from midnight
    total_seconds = (dt_obj - datetime.combine(dt_obj.date(), time.min)).total_seconds()
    return total_seconds


def parse_markdown_slides(md_content: str) -> list:
    """Parses slide information (timestamp, image) from Markdown content."""
    slides = []
    
    # DEBUG: 打印文件内容的前500个字符，用于验证读取是否正确
    print("\n=== DEBUG: 文件内容样本 ===")
    print(md_content[:500])
    print("=== 文件内容样本结束 ===\n")
    
    # 修复正则表达式：去除额外的反斜杠
    # Regex to find slide headers and their timestamps
    slide_header_pattern = re.compile(r"^## Slide (\d+) \(Timestamp: (.*?)\)", re.MULTILINE)
    
    # DEBUG: 打印正则表达式模式
    print(f"DEBUG: 使用的幻灯片标题正则表达式: {slide_header_pattern.pattern}")
    
    # Regex to find the image markdown following a header
    # Removed '^' anchor to allow finding image after potential blank lines
    image_pattern = re.compile(r"!\[(.*?)\]\((.*?)\)", re.MULTILINE)
    
    # DEBUG: 打印正则表达式模式
    print(f"DEBUG: 使用的图片链接正则表达式: {image_pattern.pattern}")

    # 尝试手动查找匹配
    test_matches = slide_header_pattern.findall(md_content)
    print(f"DEBUG: 找到 {len(test_matches)} 个幻灯片标题匹配")
    if test_matches:
        print(f"DEBUG: 前3个匹配: {test_matches[:3]}")
    
    # Store header matches first to easily find the range between headers
    header_matches = list(slide_header_pattern.finditer(md_content))
    print(f"DEBUG: 找到 {len(header_matches)} 个幻灯片标题迭代器匹配")

    for i, header_match in enumerate(header_matches):
        slide_num = int(header_match.group(1))
        timestamp_str = header_match.group(2)
        start_seconds = timestamp_to_seconds(timestamp_str)
        
        print(f"DEBUG: 处理第 {slide_num} 张幻灯片, 时间戳: {timestamp_str}")

        # Define the search range for the image: from the end of the current header
        # to the start of the next header (or end of file for the last slide)
        search_start = header_match.end()
        search_end = header_matches[i+1].start() if (i + 1) < len(header_matches) else len(md_content)
        
        # DEBUG: 打印搜索范围内的内容
        search_content = md_content[search_start:min(search_start+100, search_end)]
        print(f"DEBUG: 搜索图片的内容范围(前100字符): {search_content}")

        # Search for the image within this specific range
        image_match = image_pattern.search(md_content, search_start, search_end)
        
        if image_match:
             # DEBUG
             print(f"DEBUG: 找到图片匹配: {image_match.group(0)}")
             
             # Check if the content between header and image is only whitespace
             content_between = md_content[search_start:image_match.start()]
             if content_between.strip() == "":
                 image_md = image_match.group(0)  # 使用整个匹配而不是group(1)
                 slides.append({
                     'slide': slide_num,
                     'timestamp': timestamp_str,
                     'start_seconds': start_seconds,
                     'image_md': image_md
                 })
             else:
                 # Found non-whitespace, treat as potential issue but still add
                 print(f"Warning: Found non-whitespace content between header and image for Slide {slide_num}. Using image anyway.")
                 image_md = image_match.group(0)  # 使用整个匹配而不是group(1)
                 slides.append({
                     'slide': slide_num,
                     'timestamp': timestamp_str,
                     'start_seconds': start_seconds,
                     'image_md': image_md
                 })

        else:
            # Image not found within the expected range
            print(f"Warning: Could not find image between header for Slide {slide_num} and the next header.")
            # Optionally add slide without image data if needed
            # slides.append({
            #     'slide': slide_num,
            #     'timestamp': timestamp_str,
            #     'start_seconds': start_seconds,
            #     'image_md': None # Indicate no image found
            # })


    # Sort slides just in case they are out of order in the file
    slides.sort(key=lambda x: x['start_seconds'])
    print(f"DEBUG: 最终解析得到 {len(slides)} 张幻灯片")
    return slides

def parse_transcript(transcript_content: str) -> list:
    """Parses timestamped text segments from transcript content."""
    segments = []
    
    # 支持多种格式的转录文件
    # 1. 支持原格式: [HH:MM:SS --> HH:MM:SS] text
    # 2. 支持秒格式: [seconds.seconds - seconds.seconds] text
    # 3. 支持 fallback: 完整文本无时间戳
    
    # 优先匹配秒格式时间戳
    # 如果存在 "Utterances with timing" 段落，只在该段落内查找；否则全文件查找
    search_content = transcript_content
    if "Utterances with timing:" in transcript_content:
        parts = transcript_content.split("Utterances with timing:", 1)
        if len(parts) > 1:
            search_content = parts[1].strip()
    
    seconds_pattern = re.compile(r'^\[(\d+(?:\.\d+)?)s\s*-\s*(\d+(?:\.\d+)?)s\]\s*(.*?)$', re.MULTILINE)
    seconds_matches = list(seconds_pattern.finditer(search_content))
    if seconds_matches:
        if search_content is not transcript_content:
            print("Detected volcEngine transcript format with utterances")
        else:
            print("Detected inline seconds-based timestamps")

        print(f"DEBUG: 使用秒格式正则表达式: {seconds_pattern.pattern}")
        print(f"DEBUG: 样本内容前200字符: {search_content[:200]}")
        print(f"DEBUG: 找到 {len(seconds_matches)} 个秒格式时间戳匹配")

        for i, match in enumerate(seconds_matches[:3]):
            print(f"DEBUG: 匹配 #{i+1}: {match.group(0)}")
            print(f"DEBUG: 组1(开始时间): {match.group(1)}, 组2(结束时间): {match.group(2)}, 组3(文本): {match.group(3)[:30]}...")

        for match in seconds_matches:
            start_seconds_str, end_seconds_str, text = match.groups()
            try:
                start_seconds = float(start_seconds_str)
                end_seconds = float(end_seconds_str)

                start_time = str(timedelta(seconds=start_seconds)).split('.')[0]
                end_time = str(timedelta(seconds=end_seconds)).split('.')[0]

                if start_time.count(':') == 1:
                    start_time = f"00:{start_time}"
                if end_time.count(':') == 1:
                    end_time = f"00:{end_time}"

                segments.append({
                    'start': start_time,
                    'end': end_time,
                    'start_seconds': start_seconds,
                    'end_seconds': end_seconds,
                    'text': text.strip()
                })
            except ValueError:
                print(f"Warning: Could not parse seconds from time values: {start_seconds_str}, {end_seconds_str}")
                continue
    
    # 检查普通的时间戳格式 [HH:MM:SS --> HH:MM:SS]
    if not segments:
        print("Trying standard timestamp format [HH:MM:SS --> HH:MM:SS]")
        # 原始的正则表达式模式：捕获开始时间、结束时间和文本
        segment_pattern = re.compile(r"^\[(\d{2}:\d{2}:\d{2}) --> (\d{2}:\d{2}:\d{2})\] (.*)", re.MULTILINE)
        
        for match in segment_pattern.finditer(transcript_content):
            start_str, end_str, text = match.groups()
            start_seconds = timestamp_to_seconds(start_str)
            end_seconds = timestamp_to_seconds(end_str)
            segments.append({
                'start': start_str,
                'end': end_str,
                'start_seconds': start_seconds,
                'end_seconds': end_seconds,
                'text': text.strip()
            })
    
    # 如果没有找到任何时间戳，尝试把整个文本作为一个片段
    if not segments and transcript_content.strip():
        print("Warning: No timestamp pattern found in transcript. Treating entire content as a single segment.")
        segments.append({
            'start': "00:00:00",
            'end': "99:59:59",  # 使用一个很大的结束时间
            'start_seconds': 0.0,
            'end_seconds': 359999.0,  # 99小时59分59秒对应的秒数
            'text': transcript_content.strip()
        })
    
    print(f"Found {len(segments)} transcript segments")
    
    # DEBUG: 打印前几个解析出的片段
    if segments:
        print("\nDEBUG: 前3个转录片段:")
        for i, segment in enumerate(segments[:3]):
            print(f"片段 #{i+1}: {segment['start']} - {segment['end']}: {segment['text'][:50]}...")
    
    return segments

def integrate_files(slides_path: str, transcript_path: str, output_path: str, timestamp_format: str = "**`[{start} - {end}]`**"):
    """Reads input files, integrates data, and writes the output Markdown file."""
    try:
        print(f"DEBUG: 读取幻灯片文件: {slides_path}")
        with open(slides_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        print(f"DEBUG: 幻灯片文件大小: {len(md_content)} 字节")
    except Exception as e:
        print(f"Error reading slides file '{slides_path}': {e}")
        sys.exit(1)

    try:
        print(f"DEBUG: 读取转录文件: {transcript_path}")
        with open(transcript_path, 'r', encoding='utf-8') as f:
            transcript_content = f.read()
        print(f"DEBUG: 转录文件大小: {len(transcript_content)} 字节")
    except Exception as e:
        print(f"Error reading transcript file '{transcript_path}': {e}")
        sys.exit(1)

    slides = parse_markdown_slides(md_content)
    transcript_segments = parse_transcript(transcript_content)

    print(f"DEBUG: 解析出 {len(transcript_segments)} 个转录段落")

    if not slides:
        print("Error: No slides found in the Markdown file.")
        sys.exit(1)
    if not transcript_segments:
        print("Warning: No transcript segments found in the transcript file.")
        # Continue execution, output will just have images

    integrated_content = []

    # Add a small epsilon for float comparisons
    epsilon = 0.001

    current_transcript_index = 0
    for i, slide in enumerate(slides):
        slide_start_time = slide['start_seconds']
        # Determine the end time for this slide's text (start time of the next slide)
        slide_end_time = slides[i+1]['start_seconds'] if (i + 1) < len(slides) else float('inf')

        slide_text_segments = []
        slide_segments_with_timestamps = []

        # Find transcript segments that fall within this slide's time range
        # A segment belongs to a slide if the segment's START time is >= slide's start time
        # AND the segment's START time is < next slide's start time
        temp_index = current_transcript_index
        while temp_index < len(transcript_segments):
            segment = transcript_segments[temp_index]
            # Check if segment starts within the slide's time window
            if segment['start_seconds'] >= slide_start_time - epsilon and segment['start_seconds'] < slide_end_time - epsilon:
                slide_text_segments.append(segment['text'])
                
                # 保存带时间戳的片段
                slide_segments_with_timestamps.append({
                    'start': segment['start'],
                    'end': segment['end'],
                    'text': segment['text']
                })
                
                # Optimization: update the starting point for the next slide search
                # This assumes transcript segments are ordered by time
                current_transcript_index = temp_index + 1
                temp_index += 1
            elif segment['start_seconds'] >= slide_end_time - epsilon:
                # Segment starts after this slide ends, stop searching for this slide
                break
            else:
                # Segment starts before this slide starts (should ideally not happen if current_transcript_index is maintained)
                temp_index += 1

        # 创建带时间戳的文本
        timestamped_text_segments = []
        for segment in slide_segments_with_timestamps:
            # 使用自定义的时间戳格式
            timestamp_header = timestamp_format.format(start=segment['start'], end=segment['end'])
            timestamped_text_segments.append(f"{timestamp_header}\n{segment['text']}")
        
        # 结合该幻灯片的所有文本片段
        combined_text = "\n\n".join(timestamped_text_segments) # 添加双换行符分隔段落

        # Append to integrated content
        if slide['image_md']:
            # 添加幻灯片标题，包含时间戳
            integrated_content.append(f"## Slide {slide['slide']} (Timestamp: {slide['timestamp']})")
            integrated_content.append("\n")
            
            # 添加幻灯片图片
            integrated_content.append(slide['image_md'])
            integrated_content.append("\n") # Add space after image
            
            # 添加带时间戳的文本内容
            if combined_text:
                integrated_content.append(combined_text)
            
            # 添加分隔符，即使这个幻灯片没有文本
            integrated_content.append("\n\n---\n")
        else:
            # 处理未找到图片的情况
            integrated_content.append(f"## Slide {slide['slide']} (Timestamp: {slide['timestamp']}) - Image Missing")
            integrated_content.append("\n")
            
            # 添加带时间戳的文本内容
            if combined_text:
                integrated_content.append(combined_text)
            
            integrated_content.append("\n\n---\n")

    # Write the final output file
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(integrated_content))
        print(f"Successfully integrated transcript into: {output_path}")
    except Exception as e:
        print(f"Error writing output file '{output_path}': {e}")
        sys.exit(1)

def main():
    """Main function to parse arguments and run the integration."""
    parser = argparse.ArgumentParser(description='Integrate transcript text into a slide Markdown file.')
    parser.add_argument('slides_file', help='Input Markdown file with slide images and timestamps (e.g., slide_timestamps.md)')
    parser.add_argument('transcript_file', help='Input transcript text file with timestamps (e.g., transcript.txt)')
    parser.add_argument('output_file', nargs='?', help='Output integrated Markdown file (optional)')
    parser.add_argument('--timestamp-format', help='Custom timestamp format using {start} and {end} placeholders (default: "**`[{start} - {end}]`**")', 
                        default="**`[{start} - {end}]`**")
    parser.add_argument('--no-timestamps', action='store_true', help='Disable timestamps in the output')

    args = parser.parse_args()

    # Check if input files exist
    if not os.path.isfile(args.slides_file):
        print(f"Error: Slides file '{args.slides_file}' not found.")
        sys.exit(1)
    if not os.path.isfile(args.transcript_file):
        print(f"Error: Transcript file '{args.transcript_file}' not found.")
        sys.exit(1)

    # Generate default output filename if not provided
    output_path = args.output_file
    if not output_path:
        # 获取当前时间戳
        current_time = datetime.now().strftime('%Y%m%d%H%M%S')
        
        slides_base = os.path.basename(args.slides_file)
        slides_name, _ = os.path.splitext(slides_base)
        output_filename = f"{slides_name}_integrated_{current_time}.md"
        # Place output in the same directory as the slides file by default
        output_path = os.path.join(os.path.dirname(os.path.abspath(args.slides_file)), output_filename)
        print(f"No output file specified. Using default: {output_path}")

    # 如果用户选择禁用时间戳，使用空格式
    timestamp_format = "" if args.no_timestamps else args.timestamp_format
    
    integrate_files(args.slides_file, args.transcript_file, output_path, timestamp_format)

if __name__ == "__main__":
    main() 