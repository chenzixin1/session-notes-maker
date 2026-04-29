#!/usr/bin/env python3
"""
火山引擎语音转写工具

该脚本封装了音视频文件转写的完整流程：
1. 支持直接处理音频文件，或先从视频中抽取 MP3 并复用本地缓存;
2. 将音频上传至 Cloudflare R2（S3 兼容存储）并生成可公开访问的 URL;
3. 调用火山引擎语音识别 API 提交转写任务，支持可选参数（语言、标点、分段等）；
4. 轮询查询任务状态，获取全文与分段结果并可写入输出文件；
5. 通过 ``--check-only <task_id>`` 支持单独查询任务状态。

Usage:
    python 01_transcribe_video.py <input_media> [--output <transcript.txt>] [options]

Example:
    python 01_transcribe_video.py data/inputs/demo/刘凯宁_3min_demo.mp4 \
        --output data/inputs/demo/刘凯宁_3min_demo_transcript.txt
"""

import requests
import json
import time
import uuid
import os
import tempfile
import argparse
import base64
from moviepy.video.io.VideoFileClip import VideoFileClip
import shutil
import boto3
from botocore.client import Config
from urllib.parse import urlparse, quote

# Import configuration
try:
    import config
except ImportError:
    print("Warning: config.py not found, using default values")
    # Default configuration if config.py doesn't exist
    class config:
        APP_KEY = "YOUR_APP_KEY"
        ACCESS_KEY = "YOUR_ACCESS_KEY"
        SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
        QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
        DEFAULT_LANGUAGE = "zh"
        DEFAULT_MAX_RETRIES = 60
        DEFAULT_RETRY_DELAY = 5
        SUPPORTED_AUDIO_FORMATS = ['.mp3', '.wav', '.flac', '.ogg']
        SUPPORTED_VIDEO_FORMATS = ['.mp4', '.avi', '.mov', '.mkv']
        
        # Cloudflare R2 configuration
        R2_ENDPOINT_URL = "YOUR_R2_ENDPOINT_URL"
        R2_ACCESS_KEY_ID = "YOUR_R2_ACCESS_KEY_ID"
        R2_SECRET_ACCESS_KEY = "YOUR_R2_SECRET_ACCESS_KEY"
        R2_BUCKET_NAME = "YOUR_R2_BUCKET_NAME"
        R2_PUBLIC_URL_PREFIX = "YOUR_R2_PUBLIC_URL_PREFIX"

class R2Uploader:
    def __init__(self, endpoint_url, access_key_id, secret_access_key, bucket_name, public_url_prefix):
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket_name = bucket_name
        self.public_url_prefix = public_url_prefix
        
        # Initialize S3 client for R2
        self.s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(signature_version='s3v4')
        )
    
    def upload_file(self, file_path, custom_key=None):
        """Upload a file to Cloudflare R2 and return the public URL"""
        # If custom_key is not provided, use the filename
        key = custom_key or os.path.basename(file_path)
        
        # Add a UUID to ensure uniqueness
        filename, file_ext = os.path.splitext(key)
        unique_key = f"{filename}_{str(uuid.uuid4())[:8]}{file_ext}"
        
        try:
            print(f"DEBUG: Uploading file to R2: {file_path} -> {unique_key}")
            self.s3_client.upload_file(file_path, self.bucket_name, unique_key)
            
            # Construct the public URL — percent-encode the key so non-ASCII
            # characters (Chinese, colons, etc.) are safe for remote HTTP clients.
            public_url = f"{self.public_url_prefix.rstrip('/')}/{quote(unique_key, safe='')}"
            print(f"DEBUG: File uploaded successfully. Public URL: {public_url}")
            
            return public_url
        except Exception as e:
            print(f"ERROR: Failed to upload file to R2: {str(e)}")
            raise Exception(f"R2 upload failed: {str(e)}")

class AudioTranscriber:
    def __init__(self, api_key, r2_uploader=None, resource_id="volc.bigasr.auc"):
        self.api_key = api_key
        self.submit_url = config.SUBMIT_URL
        self.query_url = config.QUERY_URL
        self.r2_uploader = r2_uploader
        self.resource_id = resource_id
        
        # Setup cache directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(script_dir, ".audiocache")
        os.makedirs(self.cache_dir, exist_ok=True)
        
    def extract_audio_from_video(self, video_path):
        """Extract MP3 audio from a video file, using a cache."""
        video_filename_stem = os.path.splitext(os.path.basename(video_path))[0]
        cached_mp3_filename = f"{video_filename_stem}.mp3"
        cached_mp3_path = os.path.join(self.cache_dir, cached_mp3_filename)

        if os.path.exists(cached_mp3_path):
            print(f"DEBUG: Using cached MP3: {cached_mp3_path}")
            return cached_mp3_path
        
        print(f"DEBUG: Extracting audio from video to cache: {video_path} -> {cached_mp3_path}")
        try:
            video = VideoFileClip(video_path)
            video.audio.write_audiofile(cached_mp3_path, codec='mp3')
            video.close()
            print(f"DEBUG: Audio extracted and cached successfully: {cached_mp3_path}")
            return cached_mp3_path
        except Exception as e:
            if os.path.exists(cached_mp3_path):
                os.remove(cached_mp3_path)
            raise Exception(f"Failed to extract audio from video: {str(e)}")
    
    def process_input_file(self, file_path):
        """Process the input file - if MP4, extract audio first"""
        if not os.path.exists(file_path):
            raise Exception(f"File does not exist: {file_path}")
            
        file_extension = os.path.splitext(file_path)[1].lower()
        
        if file_extension in config.SUPPORTED_VIDEO_FORMATS:
            return self.extract_audio_from_video(file_path)
        elif file_extension in config.SUPPORTED_AUDIO_FORMATS:
            print(f"DEBUG: Using direct audio file: {file_path}")
            return file_path
        else:
            raise Exception(f"Unsupported file type: {file_extension}")
    
    def upload_audio_to_r2(self, audio_path):
        """Upload audio file to Cloudflare R2 and get public URL"""
        if not self.r2_uploader:
            raise Exception("R2 uploader not configured. Please provide R2 credentials.")
        
        # Upload to R2 and get the public URL
        audio_url = self.r2_uploader.upload_file(audio_path)
        return audio_url
            
    def submit_transcription_task(self, audio_path, lang="zh", punctuation=True, show_utterances=True):
        """Submit a transcription task with the audio file"""
        task_id = str(uuid.uuid4()) # This is the X-Api-Request-Id for this specific task
        print(f"DEBUG: submit_transcription_task: Generated X-Api-Request-Id (task_id): {task_id}")
        
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1"
        }
        
        # Read the audio file - size in bytes for debugging
        file_size = os.path.getsize(audio_path)
        print(f"DEBUG: submit_transcription_task: Audio file size: {file_size} bytes")
        
        # Upload audio to R2 and get public URL
        audio_url = self.upload_audio_to_r2(audio_path)
        print(f"DEBUG: submit_transcription_task: Uploaded audio file to R2. Public URL: {audio_url}")
        
        # Determine audio format - use fixed values instead of file extension
        file_extension = os.path.splitext(audio_path)[1].lower()
        if file_extension == '.mp3':
            audio_format = "mp3"
        elif file_extension == '.wav':
            audio_format = "wav"
        elif file_extension == '.ogg':
            audio_format = "ogg"
        elif file_extension == '.flac':
            audio_format = "flac"
        else:
            # Default to raw PCM if not recognized
            audio_format = "wav"  # Most compatible format as fallback
            
        print(f"DEBUG: submit_transcription_task: Using audio_format: {audio_format} for file with extension {file_extension}")
        
        # The 'config' field needs to be a JSON string, as per the error message.
        request_options = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": punctuation,
            "enable_speaker_info": show_utterances,
            "enable_channel_split": False,
            "enable_ddc": False,
            "show_utterances": show_utterances,
            "vad_segment": True,
            "lang": lang,
            "sensitive_words_filter": ""
        }

        payload = {
            "user": {
                "uid": task_id
            },
            "audio": {
                "format": audio_format,
                "url": audio_url
            },
            "request": request_options
        }
        
        print(f"DEBUG: submit_transcription_task: Request URL: {self.submit_url}")
        print(f"DEBUG: submit_transcription_task: Request Headers: {json.dumps(headers, indent=2)}")
        print(f"DEBUG: submit_transcription_task: Request Payload: {json.dumps(payload, indent=2)}")

        try:
            response = requests.post(self.submit_url, headers=headers, json=payload, timeout=30) # Added timeout
            print(f"DEBUG: submit_transcription_task: Response Status Code: {response.status_code}")
            print(f"DEBUG: submit_transcription_task: Response Headers: {json.dumps(dict(response.headers), indent=2)}")
            response_text = response.text
            print(f"DEBUG: submit_transcription_task: Response Body Text: {response_text}")

            if response.status_code != 200:
                raise Exception(f"API Error: Failed to submit transcription task. Status: {response.status_code}, Body: {response_text}")
            
            response_json = response.json()
            
            api_status_code = response.headers.get("X-Api-Status-Code")
            api_message = response.headers.get("X-Api-Message")
            print(f"DEBUG: submit_transcription_task: X-Api-Status-Code: {api_status_code}, X-Api-Message: {api_message}")

            # Handle specific error codes for better troubleshooting
            if api_status_code == "45000151":  # Invalid audio format
                raise Exception(f"API Error: Invalid audio format. Please check the audio file format and encoding. Error details: {api_message}")
            elif api_status_code == "45000002":  # Empty audio
                raise Exception(f"API Error: Empty audio file detected. Error details: {api_message}")
            elif api_status_code == "45000001":  # Invalid parameters
                raise Exception(f"API Error: Invalid parameters in the request. Error details: {api_message}")
            elif api_status_code.startswith("55"):  # Server errors
                raise Exception(f"API Error: Server-side error. Please try again later. Error details: {api_message}")
            
            # The API sends back our task_id in the X-Api-Request-Id header and X-Api-Status-Code: 20000000 for success
            # Per the docs, we need to use this same task_id (X-Api-Request-Id) when querying for results
            
            if api_status_code == "20000000":
                # If status is success, use the task_id we generated regardless of response body content
                print(f"DEBUG: submit_transcription_task: Submission successful with status code 20000000 (success). Using task_id: {task_id}")
                return task_id
            elif "request_id" in response_json:
                # Fallback for unexpected response format but with request_id
                print(f"DEBUG: submit_transcription_task: Submission response has request_id but unexpected status. Using task_id: {task_id}, Response request_id: {response_json.get('request_id')}")
                return task_id
            else:
                # If we reach here, the submission was not successful
                err_msg = f"API Error: Submission status code indicates error. X-Api-Status-Code: '{api_status_code}', Message: '{api_message}'. Response: {response_text}"
                raise Exception(err_msg)

        except requests.exceptions.RequestException as e:
            print(f"DEBUG: submit_transcription_task: RequestException: {str(e)}")
            raise Exception(f"Network error during task submission: {str(e)}")
        except json.JSONDecodeError:
            print(f"DEBUG: submit_transcription_task: JSONDecodeError. Response was not valid JSON: {response_text}")
            raise Exception(f"API Error: Failed to decode JSON response from submission. Status: {response.status_code}, Body: {response_text}")
    
    def query_transcription_result(self, task_id_for_query):
        """Query the transcription result using the task ID"""
        print(f"DEBUG: query_transcription_result: Querying for task_id: {task_id_for_query}")
        
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": task_id_for_query
        }
        
        payload = {}
        
        print(f"DEBUG: query_transcription_result: Request URL: {self.query_url}")
        print(f"DEBUG: query_transcription_result: Request Headers: {json.dumps(headers, indent=2)}")
        print(f"DEBUG: query_transcription_result: Request Payload: {json.dumps(payload, indent=2)}")

        try:
            response = requests.post(self.query_url, headers=headers, json=payload, timeout=30)
            print(f"DEBUG: query_transcription_result: Response Status Code: {response.status_code}")
            print(f"DEBUG: query_transcription_result: Response Headers: {json.dumps(dict(response.headers), indent=2)}")
            response_text = response.text
            print(f"DEBUG: query_transcription_result: Response Body Text: {response_text}")

            # Handle rate-limiting and temporary service issues
            if response.status_code in [429, 503]:
                print(f"DEBUG: query_transcription_result: Received temporary error code {response.status_code}. Will retry.")
                return None
            
            if response.status_code != 200:
                 raise Exception(f"API Error: Failed to query transcription result. Status: {response.status_code}, Body: {response_text}")

            response_json = response.json()
            
            api_status_code = response.headers.get("X-Api-Status-Code")
            api_message = response.headers.get("X-Api-Message")
            print(f"DEBUG: query_transcription_result: X-Api-Status-Code: {api_status_code}, X-Api-Message: {api_message}")

            # Check if the response contains result.text and if it's not empty - this is our success criteria
            result_text = response_json.get("result", {}).get("text")
            if result_text and result_text.strip():
                print(f"DEBUG: query_transcription_result: Found non-empty transcript text of length {len(result_text)}")
                return response_json

            # Handle specific error codes
            if api_status_code == "45000151":  # Invalid audio format during query?
                raise Exception(f"API Error: Invalid audio format reported during result query. Error details: {api_message}")
            elif api_status_code == "45000001":  # Invalid parameters 
                raise Exception(f"API Error: Invalid parameters in the query request. Error details: {api_message}")
            elif api_status_code.startswith("55"):  # Server errors
                print(f"DEBUG: query_transcription_result: Received server error {api_status_code}. Will retry.")
                return None
            
            # Task is completed with success
            if api_status_code == "20000000": 
                transcription_status = response_json.get("status") 
                print(f"DEBUG: query_transcription_result: Transcription job status from body: '{transcription_status}'")
                
                # If we received a 20000000 status but no text, it means the job is still processing
                if not result_text or not result_text.strip():
                    print(f"DEBUG: query_transcription_result: Received success status code but no transcript text. Continuing to wait.")
                    return None
                    
                if transcription_status == "SUCCESS":
                    return response_json
                elif transcription_status in ["RUNNING", "PENDING"] or transcription_status is None:
                    print(f"DEBUG: query_transcription_result: Transcription still in progress (status: {transcription_status}).")
                    return None 
                else: 
                    error_detail = response_json.get("message", api_message or "Unknown transcription error")
                    raise Exception(f"Transcription job failed or has an unexpected status '{transcription_status}'. Detail: {error_detail}. Full response: {response_text}")
            # Task is in processing state
            elif api_status_code in ["20000001", "20000002"]:
                print(f"DEBUG: query_transcription_result: Transcription in progress with status code: {api_status_code}, message: {api_message}")
                return None
            else: 
                # If we get here, we have an unexpected status code but we'll retry instead of failing
                print(f"DEBUG: query_transcription_result: Unexpected API status code: '{api_status_code}'. Will retry.")
                return None

        except requests.exceptions.RequestException as e:
            print(f"DEBUG: query_transcription_result: RequestException: {str(e)}")
            # Don't raise an exception, just return None to allow retry
            print(f"Network error during result query: {str(e)}. Will retry.")
            return None
        except json.JSONDecodeError:
            print(f"DEBUG: query_transcription_result: JSONDecodeError. Response was not valid JSON: {response_text}")
            # Don't raise an exception, just return None to allow retry
            print(f"Invalid JSON response from query: {response_text}. Will retry.")
            return None
        except Exception as e:
            print(f"DEBUG: query_transcription_result: Unexpected error: {str(e)}")
            # Don't raise an exception, just return None to allow retry
            print(f"Unexpected error during query: {str(e)}. Will retry.")
            return None

    def transcribe_audio(self, audio_path, lang, punctuation, show_utterances, max_retries, delay):
        """Transcribe audio file and wait for the result"""
        try:
            processed_audio_path = self.process_input_file(audio_path)
            
            print(f"DEBUG: transcribe_audio: Submitting task for: {processed_audio_path}")
            task_id = self.submit_transcription_task(
                processed_audio_path,
                lang=lang,
                punctuation=punctuation,
                show_utterances=show_utterances
            )
            print(f"DEBUG: transcribe_audio: Task submitted. Task ID for querying: {task_id}")
            
            start_time = time.time()
            for i in range(max_retries):
                elapsed_time = time.time() - start_time
                estimated_time_remaining = (max_retries - i) * delay
                
                print(f"DEBUG: transcribe_audio: Checking result... (attempt {i+1}/{max_retries}, "
                      f"elapsed: {elapsed_time:.1f}s, est. remaining: {estimated_time_remaining:.1f}s) "
                      f"for task_id: {task_id}")
                
                result = self.query_transcription_result(task_id)
                
                if result is not None:
                    total_time = time.time() - start_time
                    print(f"DEBUG: transcribe_audio: Transcription completed in {total_time:.1f} seconds!")
                    return result
                
                print(f"DEBUG: transcribe_audio: Transcription in progress, waiting {delay} seconds...")
                time.sleep(delay)
            
            raise Exception(f"Transcription timed out after {max_retries} retries ({max_retries * delay} seconds) for task_id: {task_id}")
        except Exception as e:
            print(f"DEBUG: transcribe_audio: Exception caught: {str(e)}")
            raise e

def format_srt_timestamp(ms):
    """Convert milliseconds to SRT timestamp format."""
    total_ms = max(int(ms), 0)
    hours = total_ms // 3600000
    minutes = (total_ms % 3600000) // 60000
    seconds = (total_ms % 60000) // 1000
    milliseconds = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def build_srt_content(result, include_speaker=True):
    """Build SRT subtitle content from the API result."""
    result_data = result.get("result", {})
    utterances = result_data.get("utterances", [])
    srt_blocks = []

    for idx, utterance in enumerate(utterances, start=1):
        start_time = utterance.get("start_time", 0)
        end_time = utterance.get("end_time", start_time)
        text_utt = (utterance.get("text") or "").strip()
        if not text_utt:
            continue

        speaker = utterance.get("speaker")
        if speaker in [None, ""]:
            speaker = utterance.get("additions", {}).get("speaker")
        if include_speaker and speaker not in [None, ""]:
            text_utt = f"说话人{speaker}: {text_utt}"

        srt_blocks.append(
            f"{idx}\n"
            f"{format_srt_timestamp(start_time)} --> {format_srt_timestamp(end_time)}\n"
            f"{text_utt}"
        )

    return "\n\n".join(srt_blocks)


def save_transcript(result, output_file=None, srt_output_file=None):
    """Save the transcript result to a file or print to stdout"""
    
    # Debug: Print the raw result structure
    print("DEBUG: Raw API response structure:")
    print(f"Result keys: {list(result.keys())}")
    if "result" in result:
        result_data = result["result"]
        print(f"Result data keys: {list(result_data.keys())}")
        
        # Check utterances structure
        utterances = result_data.get("utterances", [])
        if utterances:
            print(f"Number of utterances: {len(utterances)}")
            print("DEBUG: First utterance structure:")
            print(f"  Keys: {list(utterances[0].keys())}")
            print(f"  Content: {utterances[0]}")
            
            # Check if any utterance has speaker info
            speakers_found = [u.get("speaker") for u in utterances if "speaker" in u]
            print(f"DEBUG: Speakers found in utterances: {speakers_found}")
            print(f"DEBUG: Number of utterances with speaker info: {len(speakers_found)}")

    result_data = result.get("result", {})
    full_text = result_data.get("text", "No transcript text available in result object.")
    
    output = f"Full Transcript:\n{full_text}\n\n"
    
    utterances = result_data.get("utterances", [])
    if utterances:
        output += "Utterances with timing:\n"
        for utterance in utterances:
            start_time = utterance.get("start_time", 0) / 1000
            end_time = utterance.get("end_time", 0) / 1000
            text_utt = utterance.get("text", "")
            
            # Check if speaker information is available
            speaker = utterance.get("speaker")
            if speaker in [None, ""]:
                speaker = utterance.get("additions", {}).get("speaker")
            if speaker:
                output += f"[{start_time:.2f}s - {end_time:.2f}s] 说话人{speaker}: {text_utt}\n"
            else:
                output += f"[{start_time:.2f}s - {end_time:.2f}s] {text_utt}\n"
    
    if output_file:
        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"Transcript saved to: {output_file}")
    else:
        print("\n--- Transcript Start ---")
        print(output)
        print("--- Transcript End ---")

    if srt_output_file:
        srt_content = build_srt_content(result, include_speaker=True)
        if not srt_content.strip():
            print("Warning: No utterance-level timestamps found, SRT subtitle file was not created.")
        else:
            srt_output_dir = os.path.dirname(srt_output_file)
            if srt_output_dir:
                os.makedirs(srt_output_dir, exist_ok=True)
            with open(srt_output_file, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            print(f"SRT subtitle saved to: {srt_output_file}")

def main():
    parser = argparse.ArgumentParser(description="Convert MP3/MP4 files to transcript using Volcano Engine.")
    parser.add_argument("input_file", nargs='?', help="Path to the input audio/video file (optional if using --check-only)")
    parser.add_argument("--output", "-o", help="Path to save the transcript (optional)")
    parser.add_argument("--srt-output", help="Path to save the SRT subtitle file (optional)")
    parser.add_argument("--app-key", help="Volcano Engine App Key (override config)")
    parser.add_argument("--access-key", help="Volcano Engine Access Key (override config)")
    parser.add_argument("--lang", default=config.DEFAULT_LANGUAGE, help=f"Language code (default: {config.DEFAULT_LANGUAGE})")
    parser.add_argument("--no-punctuation", action="store_true", help="Disable punctuation in the output")
    parser.add_argument("--no-utterances", action="store_true", help="Disable utterances in the output")
    parser.add_argument("--max-retries", type=int, default=config.DEFAULT_MAX_RETRIES, 
                        help=f"Maximum number of retry attempts (default: {config.DEFAULT_MAX_RETRIES})")
    parser.add_argument("--delay", type=int, default=config.DEFAULT_RETRY_DELAY, 
                        help=f"Delay between retry attempts in seconds (default: {config.DEFAULT_RETRY_DELAY})")
    parser.add_argument("--check-only", help="Only check the status of a previously submitted task_id without submitting a new job")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable more verbose output for debugging")
    
    # Cloudflare R2 arguments
    parser.add_argument("--r2-endpoint", help="Cloudflare R2 endpoint URL (override config)")
    parser.add_argument("--r2-access-key", help="Cloudflare R2 access key ID (override config)")
    parser.add_argument("--r2-secret-key", help="Cloudflare R2 secret access key (override config)")
    parser.add_argument("--r2-bucket", help="Cloudflare R2 bucket name (override config)")
    parser.add_argument("--r2-url-prefix", help="Cloudflare R2 public URL prefix (override config)")
    
    args = parser.parse_args()
    
    key_candidates = [
        args.access_key,
        args.app_key,
        getattr(config, "ACCESS_KEY", None),
        getattr(config, "APP_KEY", None)
    ]
    api_key = next(
        (
            candidate
            for candidate in key_candidates
            if candidate and candidate.strip() and not candidate.startswith("YOUR_")
        ),
        None
    )
    
    # Check if only checking an existing task status
    if args.check_only:
        if not api_key:
            print("Error: API key not configured. Please set it in config.py or via --access-key/--app-key.")
            return 1
        
        # Create a transcriber instance without R2 uploader
        transcriber = AudioTranscriber(api_key)
        
        task_id = args.check_only
        print(f"Checking status of task: {task_id}")
        
        max_checks = args.max_retries
        delay_between_checks = args.delay
        
        for i in range(max_checks):
            print(f"Check attempt {i+1}/{max_checks}")
            try:
                result = transcriber.query_transcription_result(task_id)
                if result is not None:
                    print("Transcription completed!")
                    save_transcript(result, args.output, args.srt_output)
                    return 0
                print(f"Transcription still in progress. Waiting {delay_between_checks} seconds...")
                time.sleep(delay_between_checks)
            except Exception as e:
                print(f"Error checking task: {str(e)}")
                return 1
        
        print(f"Transcription check timed out after {max_checks} attempts")
        return 1
    
    # Regular transcription flow
    if not args.input_file:
        print("Error: No input file specified. Please provide an input file path or use --check-only.")
        return 1
    
    if not api_key:
        print("Error: API key not configured. Please set it in config.py or via --access-key/--app-key.")
        return 1
    
    # Cloudflare R2 configuration
    r2_endpoint = args.r2_endpoint or config.R2_ENDPOINT_URL
    r2_access_key_id = args.r2_access_key or config.R2_ACCESS_KEY_ID
    r2_secret_access_key = args.r2_secret_key or config.R2_SECRET_ACCESS_KEY
    r2_bucket_name = args.r2_bucket or config.R2_BUCKET_NAME
    r2_public_url_prefix = args.r2_url_prefix or config.R2_PUBLIC_URL_PREFIX
    
    # Check R2 configuration
    if (r2_endpoint == "YOUR_R2_ENDPOINT_URL" or
        r2_access_key_id == "YOUR_R2_ACCESS_KEY_ID" or
        r2_secret_access_key == "YOUR_R2_SECRET_ACCESS_KEY" or
        r2_bucket_name == "YOUR_R2_BUCKET_NAME" or
        r2_public_url_prefix == "YOUR_R2_PUBLIC_URL_PREFIX"):
        print("Error: Cloudflare R2 credentials not configured. Please set them in config.py or via command line arguments.")
        return 1
    
    # Initialize R2 uploader
    try:
        r2_uploader = R2Uploader(
            endpoint_url=r2_endpoint,
            access_key_id=r2_access_key_id,
            secret_access_key=r2_secret_access_key,
            bucket_name=r2_bucket_name,
            public_url_prefix=r2_public_url_prefix
        )
    except Exception as e:
        print(f"Error initializing R2 uploader: {str(e)}")
        return 1
        
    transcriber = AudioTranscriber(api_key, r2_uploader)
    
    punctuation_setting = not args.no_punctuation
    show_utterances_setting = not args.no_utterances
    
    try:
        print(f"DEBUG: main: Starting transcription for {args.input_file} with lang='{args.lang}', punc={punctuation_setting}, utterances={show_utterances_setting}")
        
        # First process and upload the file, get the task ID
        processed_audio_path = transcriber.process_input_file(args.input_file)
        task_id = transcriber.submit_transcription_task(
            processed_audio_path,
            lang=args.lang,
            punctuation=punctuation_setting,
            show_utterances=show_utterances_setting
        )
        
        print(f"\n======================================")
        print(f"Task submitted successfully!")
        print(f"Task ID: {task_id}")
        print(f"You can check the status later with:")
        print(f"python mp3totranscript_demo.py --check-only {task_id}")
        print(f"======================================\n")
        
        # Continue with regular polling
        result = None
        start_time = time.time()
        for i in range(args.max_retries):
            elapsed_time = time.time() - start_time
            estimated_time_remaining = (args.max_retries - i) * args.delay
            
            print(f"Checking result... (attempt {i+1}/{args.max_retries}, "
                  f"elapsed: {elapsed_time:.1f}s, est. remaining: {estimated_time_remaining:.1f}s)")
            
            result = transcriber.query_transcription_result(task_id)
            
            if result is not None:
                total_time = time.time() - start_time
                print(f"Transcription completed in {total_time:.1f} seconds!")
                break
            
            print(f"Transcription in progress, waiting {args.delay} seconds...")
            time.sleep(args.delay)
        
        if result is None:
            print(f"Transcription timed out after {args.max_retries} retries.")
            print(f"You can check the status later with:")
            print(f"python mp3totranscript_demo.py --check-only {task_id}")
            return 1
        
        save_transcript(result, args.output, args.srt_output)
        
    except Exception as e:
        print(f"Error in main: {str(e)}")
        return 1
        
    return 0

if __name__ == "__main__":
    exit(main())
