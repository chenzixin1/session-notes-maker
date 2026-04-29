"""
Utility functions for the Markdown Processor

This module contains utility functions for parsing and processing Markdown files,
handling images, and interacting with the OpenRoute API.
"""

import re
import os
import base64
import logging
from typing import List, Dict, Tuple, Optional, Any
import json
import openai
import requests
from threading import Lock

# Global variables with thread safety
total_cost = 0.0
total_cost_lock = Lock()

# Set up logging
def setup_logging(log_file: str, log_level: str, log_format: str) -> None:
    """
    Set up logging configuration.

    Args:
        log_file: Path to the log file
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Format string for log messages
    """
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=numeric_level,
        format=log_format,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

def parse_markdown(md_content: str) -> List[Dict[str, Any]]:
    """
    Parse markdown content and identify slide-transcript segments.

    A segment consists of an image followed by text until the next image.
    The document title and any text before the first image is preserved as a special segment.
    This function specifically looks for image tags in the format ![...](...)
    and ignores section headings like "## Slide X (Timestamp: ...)"

    Args:
        md_content: The markdown content as a string

    Returns:
        A list of dictionaries, each containing:
        - 'images': List of image markdown references
        - 'text': The text content following the images
    """
    # Regular expression to match markdown image syntax: ![alt text](image_path)
    image_pattern = r'!\[[^\]]*\]\([^)]+\)'
    slide_header_pattern = re.compile(r'(^##\s+Slide\s+\d+.*$)', re.MULTILINE)

    # Find all images in the content
    images = re.findall(image_pattern, md_content)

    # If no images found, return a single segment with all text
    if not images:
        return [{'images': [], 'text': md_content}]

    # Split the content by images
    parts = re.split(image_pattern, md_content)

    segments = []

    # Handle text before the first image (document title, introduction, etc.)
    intro_text = parts[0]
    first_header = None
    first_header_matches = list(slide_header_pattern.finditer(parts[0]))
    if first_header_matches:
        first_header = first_header_matches[-1].group(1).strip()
        intro_text = parts[0][:first_header_matches[-1].start()]

    if intro_text.strip():
        segments.append({
            'images': [],
            'text': intro_text.strip(),
            'header': None
        })

    # Process each image and the text that follows it
    for i, image in enumerate(images):
        if i == 0:
            header = first_header
        else:
            header = None
            header_matches = list(slide_header_pattern.finditer(parts[i]))
            if header_matches:
                header = header_matches[-1].group(1).strip()

        # If this is the last image, the text is everything after it
        if i == len(images) - 1:
            text = parts[i+1]
        else:
            # Otherwise, the text is everything until the next image
            text = parts[i+1]
        
        # Remove any "## Slide X" headers from the text as we're segmenting by images
        text = re.sub(r'##\s+Slide\s+\d+.*?\n', '', text)
        
        segments.append({
            'images': [image],
            'text': text.strip(),
            'header': header
        })

    return segments

def create_temp_markdown(segments: List[Dict[str, Any]], output_path: str) -> None:
    """
    Create a temporary markdown file with segment markers.

    Args:
        segments: List of segment dictionaries
        output_path: Path to save the temporary markdown file
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("# 演讲稿分段预览\n\n")
        f.write("请检查以下分段是否正确。每个分段包含一个或多个图片，后跟相关的演讲文本。\n")
        f.write("您可以编辑此文件以调整分段，然后保存并关闭。\n\n")

        for i, segment in enumerate(segments):
            f.write(f"## 分段 {i+1}\n\n")
            f.write("<!-- 分段开始标记，请勿删除 -->\n\n")

            if segment.get('header'):
                f.write(f"{segment['header']}\n\n")

            # Write images
            for image in segment['images']:
                f.write(f"{image}\n\n")

            # Write text
            f.write(segment['text'].strip() + "\n\n")

            f.write("<!-- 分段结束标记，请勿删除 -->\n\n")
            f.write("---\n\n")

def read_temp_markdown(temp_path: str) -> List[Dict[str, Any]]:
    """
    Read the temporary markdown file and extract segments.

    Args:
        temp_path: Path to the temporary markdown file

    Returns:
        List of segment dictionaries
    """
    with open(temp_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split by segment markers
    segment_pattern = r'<!-- 分段开始标记，请勿删除 -->(.*?)<!-- 分段结束标记，请勿删除 -->'
    segment_matches = re.findall(segment_pattern, content, re.DOTALL)

    segments = []
    for segment_content in segment_matches:
        header = None
        header_match = re.search(r'^\s*(##\s+Slide\s+\d+.*)$', segment_content, re.MULTILINE)
        if header_match:
            header = header_match.group(1).strip()

        # Extract images
        image_pattern = r'(!\[[^\]]*\]\([^)]+\))'
        images = re.findall(image_pattern, segment_content)

        # Extract text (everything after the last image)
        text = segment_content
        if header:
            text = text.replace(header, '', 1)
        for image in images:
            text = text.replace(image, '')

        segments.append({
            'images': images,
            'text': text.strip(),
            'header': header
        })

    return segments

def encode_image(image_path: str) -> str:
    """
    Encode an image file as base64.

    Args:
        image_path: Path to the image file

    Returns:
        Base64-encoded image data
    """
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def find_matching_image(slide_number: int, base_dir: str) -> str:
    """
    Find a matching image file in ppt_pics directory for a slide number.
    
    Args:
        slide_number: Slide number to match
        base_dir: Base directory containing the ppt_pics folder
        
    Returns:
        Full path to the matching image file, or empty string if not found
    """
    # Construct path to ppt_pics directory
    ppt_pics_dir = os.path.join(base_dir, "ppt_pics")
    
    # Check if directory exists
    if not os.path.isdir(ppt_pics_dir):
        logging.warning(f"ppt_pics directory not found at {ppt_pics_dir}")
        return ""
    
    # List all PNG files in the directory
    try:
        png_files = [f for f in os.listdir(ppt_pics_dir) if f.endswith('.png')]
        
        # Sort the files to ensure we get them in order
        png_files.sort()
        
        # If slide number is valid and within range of available files
        if slide_number > 0 and slide_number <= len(png_files):
            # Return the path to the corresponding file (1-indexed to 0-indexed)
            return os.path.join(ppt_pics_dir, png_files[slide_number - 1])
        elif png_files:
            # Log warning and return first image as fallback
            logging.warning(f"Slide {slide_number} out of range. Using first available image.")
            return os.path.join(ppt_pics_dir, png_files[0])
        else:
            logging.warning(f"No PNG files found in {ppt_pics_dir}")
            return ""
    except Exception as e:
        logging.error(f"Error finding matching image: {e}")
        return ""

def extract_image_path(image_markdown: str, base_dir: str = "") -> str:
    """
    Extract the image path from a markdown image reference and map to actual file.

    Args:
        image_markdown: Markdown image reference (![alt text](image_path))
        base_dir: Base directory for resolving relative paths

    Returns:
        The actual image path
    """
    # Extract the path from markdown
    match = re.search(r'!\[([^\]]*)\]\(([^)]+)\)', image_markdown)
    if not match:
        return ""
    
    alt_text = match.group(1)
    path_in_md = match.group(2)
    
    # First check if path exists directly
    full_path = os.path.join(base_dir, path_in_md) if base_dir else path_in_md
    if os.path.exists(full_path):
        return full_path
    
    # Try to extract slide number from different formats
    slide_number = None
    
    # Format 1: "Slide X at HH:MM:SS.sss"
    slide_match = re.search(r'Slide\s+(\d+)\s+at', alt_text)
    if slide_match:
        slide_number = int(slide_match.group(1))
        logging.info(f"Found slide number {slide_number} from 'Slide X at' format")
    
    # Format 2: "幻灯片X" in alt text
    if not slide_number:
        slide_match = re.search(r'幻灯片(\d+)', alt_text)
        if slide_match:
            slide_number = int(slide_match.group(1))
            logging.info(f"Found slide number {slide_number} from '幻灯片X' in alt text")
    
    # Format 3: "幻灯片X" in filename
    if not slide_number:
        slide_match = re.search(r'幻灯片(\d+)', path_in_md)
        if slide_match:
            slide_number = int(slide_match.group(1))
            logging.info(f"Found slide number {slide_number} from '幻灯片X' in filename")
    
    # If we found a slide number and have a base directory
    if slide_number and base_dir:
        # Find matching image in ppt_pics
        actual_path = find_matching_image(slide_number, base_dir)
        if actual_path:
            logging.info(f"Mapped slide {slide_number} to file: {actual_path}")
            return actual_path
    
    # If no match found or mapping failed, log warning and return original path
    logging.warning(f"Could not map image to actual file: {image_markdown}")
    return path_in_md

def estimate_tokens(text: str) -> int:
    """
    Estimate the number of tokens in a text.

    This is a rough estimation based on the number of words.
    For English text, a common rule of thumb is 4 characters per token.
    For Chinese text, each character is roughly one token.

    Args:
        text: The text to estimate tokens for

    Returns:
        Estimated number of tokens
    """
    # Count Chinese characters
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))

    # Count non-Chinese words
    non_chinese_text = re.sub(r'[\u4e00-\u9fff]', '', text)
    non_chinese_words = len(re.findall(r'\b\w+\b', non_chinese_text))

    # Estimate tokens (1 token per Chinese character, 1.3 tokens per non-Chinese word)
    return chinese_chars + int(non_chinese_words * 1.3)

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimate the cost of an API call based on the model and token counts.

    Args:
        model: The model name
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens (estimated)

    Returns:
        Estimated cost in USD
    """
    # Pricing per 1M tokens (in USD) for common models
    # These are approximate and may change
    pricing = {
        "google/gemini-2.5-flash-preview": {"input": 0.35, "output": 1.05},
        "openai/gpt-4o": {"input": 5.0, "output": 15.0},
        "anthropic/claude-3-opus": {"input": 15.0, "output": 75.0},
        "anthropic/claude-3-sonnet": {"input": 3.0, "output": 15.0},
        "anthropic/claude-3-haiku": {"input": 0.25, "output": 1.25},
        # Default fallback pricing
        "default": {"input": 5.0, "output": 15.0}
    }

    # Get pricing for the specified model, or use default
    model_pricing = pricing.get(model, pricing["default"])

    # Calculate cost
    input_cost = (input_tokens / 1000000) * model_pricing["input"]
    output_cost = (output_tokens / 1000000) * model_pricing["output"]

    return input_cost + output_cost

def get_client(api_key: str, base_url: str, site_url: str = "", site_name: str = "") -> openai.OpenAI:
    """
    Create an OpenAI client configured for OpenRoute.

    Args:
        api_key: OpenRoute API key
        base_url: OpenRoute base URL
        site_url: Site URL for OpenRoute rankings (optional)
        site_name: Site name for OpenRoute rankings (optional)

    Returns:
        Configured OpenAI client
    """
    client = openai.OpenAI(
        api_key=api_key,
        base_url=base_url
    )

    # Set up extra headers for OpenRoute
    client.extra_headers = {}
    if site_url:
        client.extra_headers["HTTP-Referer"] = site_url
    if site_name:
        client.extra_headers["X-Title"] = site_name

    return client

def call_llm_with_image(
    client: openai.OpenAI,
    model: str,
    prompt: str,
    image_path: str,
    max_tokens: int = 1000,
    yes_to_all: bool = False
) -> str:
    """
    Call the LLM with an image and prompt.

    Args:
        client: OpenAI client
        model: Model name
        prompt: Text prompt
        image_path: Path to the image file
        max_tokens: Maximum number of tokens to generate

    Returns:
        The model's response
    """
    # Encode the image
    base64_image = encode_image(image_path)

    # Log the prompt
    logging.info(f"Sending prompt to {model}:")
    logging.info(prompt)

    # Estimate tokens and cost
    prompt_tokens = estimate_tokens(prompt)
    estimated_output_tokens = max_tokens
    estimated_cost = estimate_cost(model, prompt_tokens, estimated_output_tokens)

    logging.info(f"Model: {model}")
    logging.info(f"Input tokens: {prompt_tokens}")
    logging.info(f"Estimated output tokens: {estimated_output_tokens}")
    logging.info(f"Estimated cost: ${estimated_cost:.6f}")

    # Get user confirmation if not yes_to_all
    print(f"\nEstimated cost: ${estimated_cost:.6f}")
    if not yes_to_all:
        confirmation = input("Do you want to proceed with this API call? (y/n): ")
        if confirmation.lower() != 'y':
            logging.info("API call cancelled by user")
            return "API call cancelled by user"
        logging.info("User confirmed API call")
    else:
        print("Automatically confirming API call (--yes flag is set)")
        logging.info("Automatically confirmed API call (--yes flag is set)")

    try:
        # Make the API call
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=max_tokens
        )

        logging.info("Response received from API")

        # Update total cost with actual usage if available - thread safe
        global total_cost
        with total_cost_lock:
            if hasattr(response, 'usage'):
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens
                actual_cost = estimate_cost(model, input_tokens, output_tokens)
                total_cost += actual_cost
                logging.info(f"Actual cost: ${actual_cost:.6f}")
                logging.info(f"Total cost so far: ${total_cost:.6f}")
            else:
                # If usage not available, use the estimate
                total_cost += estimated_cost
                logging.info(f"Using estimated cost: ${estimated_cost:.6f}")
                logging.info(f"Total cost so far: ${total_cost:.6f}")

        # Extract and return the response text
        return response.choices[0].message.content

    except Exception as e:
        logging.error(f"Error calling API: {e}")
        return f"Error: {e}"

def get_openrouter_usage(api_key: str) -> Dict[str, Any]:
    """
    Get usage information from OpenRouter API.

    Args:
        api_key: OpenRouter API key

    Returns:
        Dictionary containing usage information
    """
    try:
        headers = {
            "Authorization": f"Bearer {api_key}"
        }
        response = requests.get("https://openrouter.ai/api/v1/auth/key", headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            logging.error(f"Error getting OpenRouter usage: {response.status_code} {response.text}")
            return {"error": f"Status code: {response.status_code}"}
    except Exception as e:
        logging.error(f"Exception getting OpenRouter usage: {e}")
        return {"error": str(e)}

def call_llm_with_text(
    client: openai.OpenAI,
    model: str,
    prompt: str,
    max_tokens: int = 1000,
    yes_to_all: bool = False
) -> str:
    """
    Call the LLM with a text prompt.

    Args:
        client: OpenAI client
        model: Model name
        prompt: Text prompt
        max_tokens: Maximum number of tokens to generate

    Returns:
        The model's response
    """
    # Log the prompt
    logging.info(f"Sending prompt to {model}:")
    logging.info(prompt)

    # Estimate tokens and cost
    prompt_tokens = estimate_tokens(prompt)
    estimated_output_tokens = max_tokens
    estimated_cost = estimate_cost(model, prompt_tokens, estimated_output_tokens)

    logging.info(f"Model: {model}")
    logging.info(f"Input tokens: {prompt_tokens}")
    logging.info(f"Estimated output tokens: {estimated_output_tokens}")
    logging.info(f"Estimated cost: ${estimated_cost:.6f}")

    # Get user confirmation if not yes_to_all
    print(f"\nEstimated cost: ${estimated_cost:.6f}")
    if not yes_to_all:
        confirmation = input("Do you want to proceed with this API call? (y/n): ")
        if confirmation.lower() != 'y':
            logging.info("API call cancelled by user")
            return "API call cancelled by user"
        logging.info("User confirmed API call")
    else:
        print("Automatically confirming API call (--yes flag is set)")
        logging.info("Automatically confirmed API call (--yes flag is set)")

    try:
        # Make the API call
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens
        )

        logging.info("Response received from API")

        # Update total cost with actual usage if available - thread safe
        global total_cost
        with total_cost_lock:
            if hasattr(response, 'usage'):
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens
                actual_cost = estimate_cost(model, input_tokens, output_tokens)
                total_cost += actual_cost
                logging.info(f"Actual cost: ${actual_cost:.6f}")
                logging.info(f"Total cost so far: ${total_cost:.6f}")
            else:
                # If usage not available, use the estimate
                total_cost += estimated_cost
                logging.info(f"Using estimated cost: ${estimated_cost:.6f}")
                logging.info(f"Total cost so far: ${total_cost:.6f}")

        # Extract and return the response text
        return response.choices[0].message.content

    except Exception as e:
        logging.error(f"Error calling API: {e}")
        return f"Error: {e}"
