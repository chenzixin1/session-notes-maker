"""
从演讲视频中抽取关键帧并生成带时间戳的 Markdown 索引。

该脚本属于流程的第 02 步：
1. 按固定时间间隔读取视频画面，用低清灰度图做幻灯片变化检测
2. 仅把检测出的幻灯片起始帧保存为高清图片到 `ppt_pics/`
3. 生成 `ppt_timestamps.md`，记录每张幻灯片的时间戳与预览图

Usage:
    python 02_extract_slide_timestamps.py input_video.mp4 [-o OUTPUT_DIR] [-i INTERVAL] [-t THRESHOLD]
        [--md_name MD_NAME] [-I | --interactive] [--rect_file PATH] [--force_reselect]
        [--ppt_rect x1,y1,x2,y2] [--full-frame] [--max_duration SEC]
        [--legacy_extract_all] [--ssim_max_side INT] [--output_max_side INT]
        [--detect-width WIDTH] [--detection-backend hybrid-keyframe|accurate]

Example (auto):
    python 02_extract_slide_timestamps.py data/inputs/GDB/media/GDB.mp4 -o data/outputs/GDB/GDB_frames

Example (interactive, draw the PPT box with the mouse):
    python 02_extract_slide_timestamps.py data/inputs/GDB/media/GDB.mp4 -I
    # The chosen rectangle is saved as 'GDB.ppt_rect.json' next to the video and
    # will be auto-loaded next time you run the script on the same file.
"""

import cv2
import os
import json
import argparse
import re
import shutil
import subprocess
import tempfile
from skimage.metrics import structural_similarity as ssim
import numpy as np
import datetime
import concurrent.futures  # Added for threading
from typing import Dict, List, Optional, Tuple

try:
    import torch
    from pytorch_msssim import ssim as torch_ssim
except ImportError:
    torch = None
    torch_ssim = None

def format_time(seconds):
    """Formats seconds into HH:MM:SS.ms string."""
    delta = datetime.timedelta(seconds=seconds)
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    milliseconds = int(delta.microseconds / 1000)
    return f"{hours:02}:{minutes:02}:{seconds_part:02}.{milliseconds:03}"

def _default_rect_file(video_path: str) -> str:
    """视频同名 sidecar JSON，保存用户手画的 PPT 矩形。"""
    video_dir = os.path.dirname(os.path.abspath(video_path))
    base = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(video_dir, f"{base}.ppt_rect.json")


def load_rect_file(rect_file: str) -> Optional[Tuple[float, float, float, float]]:
    """从 JSON 中读取相对坐标 (x1, y1, x2, y2)。"""
    if not rect_file or not os.path.isfile(rect_file):
        return None
    try:
        with open(rect_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        rect = data.get("rect_relative")
        if rect and len(rect) == 4:
            return tuple(float(v) for v in rect)  # type: ignore[return-value]
    except Exception as e:
        print(f"Warning: failed to read rect file {rect_file}: {e}")
    return None


def save_rect_file(
    rect_file: str,
    video_path: str,
    rect_relative: Tuple[float, float, float, float],
    frame_shape: Tuple[int, int],
    rect_absolute: Optional[Tuple[int, int, int, int]] = None,
) -> None:
    """把用户画的矩形保存下来，方便下次复用。"""
    payload = {
        "video": os.path.abspath(video_path),
        "frame_shape": list(frame_shape),  # (height, width)
        "rect_relative": list(rect_relative),  # (x1, y1, x2, y2) in [0, 1]
        "rect_absolute": list(rect_absolute) if rect_absolute else None,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(rect_file)), exist_ok=True)
        with open(rect_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"Saved PPT rect to: {rect_file}")
    except Exception as e:
        print(f"Warning: failed to save rect file {rect_file}: {e}")


def _sample_frames_for_preview(
    video_path: str,
    num_samples: int = 5,
) -> List[Tuple[str, np.ndarray]]:
    """从视频里均匀抽若干帧用于预览。返回 [(label, frame)]。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: could not open video for preview: {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = total_frames / fps if fps > 0 else 0.0

    if total_frames <= 0 or duration_sec <= 0:
        cap.release()
        return []

    # 在 [5%, 95%] 区间均匀抽样，避免片头片尾黑屏/转场
    ratios: List[float]
    if num_samples <= 1:
        ratios = [0.3]
    else:
        ratios = [0.05 + (0.9 * i / (num_samples - 1)) for i in range(num_samples)]

    samples: List[Tuple[str, np.ndarray]] = []
    for ratio in ratios:
        t_sec = duration_sec * ratio
        frame_id = int(t_sec * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
        ret, frame = cap.read()
        if ret and frame is not None:
            samples.append((format_time(t_sec), frame))

    cap.release()
    return samples


class _InteractiveRectSelector:
    """OpenCV 交互窗口：左右键切换预览帧，鼠标左键拖拽画框。"""

    MAX_DISPLAY_WIDTH = 1600
    MAX_DISPLAY_HEIGHT = 900

    def __init__(
        self,
        frames: List[Tuple[str, np.ndarray]],
        window_name: str = "Draw PPT Area",
        initial_rect_relative: Optional[Tuple[float, float, float, float]] = None,
    ):
        if not frames:
            raise ValueError("No frames provided for interactive selection.")
        self.frames = frames
        self.window_name = window_name
        self.current_idx = 0

        self._display_scale = self._compute_display_scale(frames[0][1])
        self._drawing = False
        self._start_pt: Optional[Tuple[int, int]] = None
        # rect in ORIGINAL (un-scaled) frame coords, (x1, y1, x2, y2)
        self._rect_orig: Optional[Tuple[int, int, int, int]] = None

        if initial_rect_relative is not None:
            h, w = frames[0][1].shape[:2]
            x1 = int(initial_rect_relative[0] * w)
            y1 = int(initial_rect_relative[1] * h)
            x2 = int(initial_rect_relative[2] * w)
            y2 = int(initial_rect_relative[3] * h)
            self._rect_orig = (x1, y1, x2, y2)

    def _compute_display_scale(self, frame: np.ndarray) -> float:
        h, w = frame.shape[:2]
        scale = 1.0
        if w > self.MAX_DISPLAY_WIDTH:
            scale = min(scale, self.MAX_DISPLAY_WIDTH / w)
        if h > self.MAX_DISPLAY_HEIGHT:
            scale = min(scale, self.MAX_DISPLAY_HEIGHT / h)
        return scale

    def _to_display(self, xy: Tuple[int, int]) -> Tuple[int, int]:
        return int(xy[0] * self._display_scale), int(xy[1] * self._display_scale)

    def _to_original(self, xy: Tuple[int, int]) -> Tuple[int, int]:
        if self._display_scale <= 0:
            return xy
        return int(xy[0] / self._display_scale), int(xy[1] / self._display_scale)

    def _mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self._drawing = True
            ox, oy = self._to_original((x, y))
            self._start_pt = (ox, oy)
            self._rect_orig = (ox, oy, ox, oy)
        elif event == cv2.EVENT_MOUSEMOVE and self._drawing and self._start_pt:
            ox, oy = self._to_original((x, y))
            self._rect_orig = (self._start_pt[0], self._start_pt[1], ox, oy)
        elif event == cv2.EVENT_LBUTTONUP and self._start_pt:
            self._drawing = False
            ox, oy = self._to_original((x, y))
            self._rect_orig = (self._start_pt[0], self._start_pt[1], ox, oy)

    def _render(self) -> np.ndarray:
        label, frame = self.frames[self.current_idx]
        if self._display_scale != 1.0:
            disp = cv2.resize(
                frame,
                (int(frame.shape[1] * self._display_scale), int(frame.shape[0] * self._display_scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            disp = frame.copy()

        if self._rect_orig is not None:
            x1, y1, x2, y2 = self._rect_orig
            px1, py1 = self._to_display((min(x1, x2), min(y1, y2)))
            px2, py2 = self._to_display((max(x1, x2), max(y1, y2)))
            cv2.rectangle(disp, (px1, py1), (px2, py2), (0, 255, 0), 2)
            # semi-transparent fill
            overlay = disp.copy()
            cv2.rectangle(overlay, (px1, py1), (px2, py2), (0, 255, 0), -1)
            disp = cv2.addWeighted(overlay, 0.15, disp, 0.85, 0)

        # Header / instructions overlay
        h_disp = disp.shape[0]
        lines = [
            f"Frame {self.current_idx + 1}/{len(self.frames)}  t={label}   scale={self._display_scale:.2f}",
            "Drag mouse to draw PPT area | [<-/->] prev/next frame | [r] reset | [ENTER] confirm | [ESC] cancel",
        ]
        if self._rect_orig is not None:
            x1, y1, x2, y2 = self._rect_orig
            x1n, x2n = sorted((x1, x2))
            y1n, y2n = sorted((y1, y2))
            h_full, w_full = self.frames[self.current_idx][1].shape[:2]
            lines.append(
                f"rect abs=({x1n},{y1n})-({x2n},{y2n})  size={x2n - x1n}x{y2n - y1n}  "
                f"rel=({x1n / w_full:.3f},{y1n / h_full:.3f})-({x2n / w_full:.3f},{y2n / h_full:.3f})"
            )

        overlay = disp.copy()
        bar_h = 22 * len(lines) + 10
        cv2.rectangle(overlay, (0, 0), (disp.shape[1], bar_h), (0, 0, 0), -1)
        disp = cv2.addWeighted(overlay, 0.5, disp, 0.5, 0)
        for i, line in enumerate(lines):
            cv2.putText(
                disp,
                line,
                (10, 22 + i * 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
        return disp

    def run(self) -> Optional[Tuple[float, float, float, float]]:
        cv2.namedWindow(self.window_name, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(self.window_name, self._mouse_callback)
        try:
            while True:
                disp = self._render()
                cv2.imshow(self.window_name, disp)
                key = cv2.waitKey(20) & 0xFF
                # ESC / q to cancel
                if key in (27, ord("q")):
                    return None
                # ENTER / SPACE to confirm
                if key in (13, 10, 32):
                    if self._rect_orig is None:
                        print("Please draw a rectangle before confirming (or press ESC to cancel).")
                        continue
                    x1, y1, x2, y2 = self._rect_orig
                    x1n, x2n = sorted((x1, x2))
                    y1n, y2n = sorted((y1, y2))
                    if x2n - x1n < 5 or y2n - y1n < 5:
                        print("Rectangle is too small, please redraw.")
                        self._rect_orig = None
                        continue
                    h_full, w_full = self.frames[self.current_idx][1].shape[:2]
                    return (
                        x1n / w_full,
                        y1n / h_full,
                        x2n / w_full,
                        y2n / h_full,
                    )
                # r to reset
                if key == ord("r"):
                    self._rect_orig = None
                # left / previous
                if key in (81, ord(","), ord("a"), ord("p")):  # LEFT arrow on Linux=81
                    self.current_idx = (self.current_idx - 1) % len(self.frames)
                # right / next
                if key in (83, ord("."), ord("d"), ord("n")):  # RIGHT arrow on Linux=83
                    self.current_idx = (self.current_idx + 1) % len(self.frames)
        finally:
            cv2.destroyWindow(self.window_name)
            # Extra waitKey so the window actually closes on macOS
            for _ in range(3):
                cv2.waitKey(1)


def interactive_select_ppt_rect(
    video_path: str,
    initial_rect_relative: Optional[Tuple[float, float, float, float]] = None,
    num_samples: int = 5,
) -> Optional[Tuple[float, float, float, float]]:
    """打开交互窗口让用户手动画出 PPT 区域，返回相对坐标 (x1, y1, x2, y2)。"""
    print(f"[Interactive] Sampling preview frames from: {video_path}")
    frames = _sample_frames_for_preview(video_path, num_samples=num_samples)
    if not frames:
        print("[Interactive] Could not sample frames; skipping interactive selection.")
        return None

    print(
        "[Interactive] A preview window will open.\n"
        "  - Drag with left mouse button to draw the PPT region\n"
        "  - Left/Right arrow (or 'a'/'d', ',' / '.') to switch preview frames\n"
        "  - Press 'r' to reset the rectangle\n"
        "  - Press ENTER or SPACE to confirm, ESC or 'q' to cancel"
    )
    selector = _InteractiveRectSelector(
        frames=frames,
        window_name=f"Draw PPT Area - {os.path.basename(video_path)}",
        initial_rect_relative=initial_rect_relative,
    )
    rect_rel = selector.run()
    if rect_rel is None:
        print("[Interactive] Selection cancelled.")
    else:
        print(f"[Interactive] Selected PPT rect (relative): {rect_rel}")
    return rect_rel


def _clamp_rect(x: int, y: int, w: int, h: int, frame_width: int, frame_height: int) -> Tuple[int, int, int, int]:
    """Clamp rectangle coordinates within frame bounds."""
    x = max(0, min(x, frame_width - 1)) if frame_width else 0
    y = max(0, min(y, frame_height - 1)) if frame_height else 0
    w = max(0, min(w, frame_width - x)) if frame_width else 0
    h = max(0, min(h, frame_height - y)) if frame_height else 0
    return x, y, w, h


def _detect_ppt_rect_in_single_frame(
    frame: np.ndarray,
) -> Dict[str, Optional[Tuple[int, int, int, int]]]:
    """
    单帧 PPT 区域检测，仅基于几何特征，用于在截帧过程中动态更新截图区域。
    """
    height, width = frame.shape[:2]
    total_area = width * height

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    rect_candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if total_area <= 0 or area < 0.02 * total_area:
            continue
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        x, y, w, h = cv2.boundingRect(approx)
        if w < 0.1 * width or h < 0.1 * height:
            continue
        if w * h > 0.95 * total_area:
            continue

        rect_candidates.append(
            {
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "area": float(w * h),
            }
        )

    layout = {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (height, width)}

    if not rect_candidates:
        full_rect = _clamp_rect(0, 0, width, height, width, height)
        layout.update({"ppt_rect": full_rect})
        return layout

    margin_x = int(0.03 * width)
    margin_y = int(0.03 * height)
    ppt_like = []
    for r in rect_candidates:
        ar = r["w"] / float(r["h"]) if r["h"] > 0 else 0.0
        area_ratio = r["area"] / float(total_area) if total_area > 0 else 0.0

        touches_border = (
            r["x"] <= margin_x
            or r["y"] <= margin_y
            or (r["x"] + r["w"]) >= (width - margin_x)
            or (r["y"] + r["h"]) >= (height - margin_y)
        )

        if not touches_border and 0.2 <= area_ratio <= 0.9 and 1.2 <= ar <= 2.2:
            cx = r["x"] + r["w"] / 2.0
            ppt_like.append(
                {
                    **r,
                    "aspect_ratio": ar,
                    "area_ratio": area_ratio,
                    "cx": cx,
                }
            )

    def _score(rc):
        ar_diff = abs(rc["aspect_ratio"] - (16.0 / 9.0))
        area_diff = abs(rc["area_ratio"] - 0.55)
        center_penalty = abs(rc["cx"] - width / 2.0) / float(width)
        return ar_diff * 2.0 + area_diff + center_penalty

    if ppt_like:
        chosen = min(ppt_like, key=_score)
    else:
        chosen = max(rect_candidates, key=lambda r: r["area"])

    ppt_rect = _clamp_rect(chosen["x"], chosen["y"], chosen["w"], chosen["h"], width, height)

    # 根据 PPT 的横向位置，粗略推断讲者区在左还是右（只用于 is_mixed 标记）
    if ppt_rect[0] > width // 2:
        video_rect = _clamp_rect(0, 0, ppt_rect[0], height, width, height)
    elif ppt_rect[0] + ppt_rect[2] < width // 2:
        video_rect = _clamp_rect(ppt_rect[0] + ppt_rect[2], 0, width - (ppt_rect[0] + ppt_rect[2]), height, width, height)
    else:
        video_rect = None

    layout.update(
        {
            "ppt_rect": ppt_rect,
            "video_rect": video_rect,
            "is_mixed": video_rect is not None,
        }
    )
    return layout

def full_frame_layout(video_path: str) -> Dict[str, Optional[Tuple[int, int, int, int]]]:
    """Use the entire frame as the extraction region (no PPT/speaker split)."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Warning: Could not open video file for dimensions: {video_path}")
        return {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (0, 0)}
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if width <= 0 or height <= 0:
        return {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (0, 0)}
    ppt_rect = _clamp_rect(0, 0, width, height, width, height)
    print(f"Full-frame mode: using entire picture {width}x{height} (crop rect={ppt_rect}).")
    return {
        "is_mixed": False,
        "ppt_rect": ppt_rect,
        "video_rect": None,
        "frame_shape": (height, width),
    }


def detect_mixed_layout(
    video_path: str,
    sample_duration: float = 60.0,
    manual_rect: Optional[Tuple[float, float, float, float]] = None,
) -> Dict[str, Optional[Tuple[int, int, int, int]]]:
    """Detect whether the video frame contains separate PPT and speaker regions.

    Returns a dictionary with keys:
        - is_mixed: bool, True if both PPT and video regions detected
        - ppt_rect: (x, y, w, h) of the detected PPT area when mixed, else None
        - video_rect: (x, y, w, h) of the speaker/video area when mixed, else None
        - frame_shape: (height, width)
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Warning: Could not open video file for layout detection: {video_path}")
        return {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (0, 0)}

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or fps * sample_duration
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # 如果提供了手动的相对坐标矩形，直接使用它作为全程的 PPT 区域
    if manual_rect is not None:
        x1_rel, y1_rel, x2_rel, y2_rel = manual_rect
        # 归一化 & 排序，避免用户输入 0.8,0.1,0.1,0.9 这种
        x1_rel, x2_rel = sorted([max(0.0, min(1.0, x1_rel)), max(0.0, min(1.0, x2_rel))])
        y1_rel, y2_rel = sorted([max(0.0, min(1.0, y1_rel)), max(0.0, min(1.0, y2_rel))])

        x1 = int(round(x1_rel * width))
        y1 = int(round(y1_rel * height))
        x2 = int(round(x2_rel * width))
        y2 = int(round(y2_rel * height))
        ppt_rect = _clamp_rect(x1, y1, x2 - x1, y2 - y1, width, height)

        layout = {
            "is_mixed": True,
            "ppt_rect": ppt_rect,
            "video_rect": None,
            "frame_shape": (height, width),
        }
        print(f"Using manual PPT rect (relative {manual_rect}) -> abs {ppt_rect}")
        cap.release()
        return layout

    max_frames_to_process = int(min(total_frames, fps * sample_duration))
    sample_stride = max(1, int(round(fps / 2)))  # ~2 fps sampling

    small_width = 320
    small_height = int(height * (small_width / width)) if width else 180

    mean = None
    M2 = None
    count = 0
    first_frame = None
    frame_idx = 0

    while frame_idx < max_frames_to_process:
        ret, frame = cap.read()
        if not ret:
            break
        if first_frame is None:
            first_frame = frame.copy()
        if frame_idx % sample_stride == 0:
            resized = cv2.resize(frame, (small_width, small_height))
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if mean is None:
                mean = gray
                M2 = np.zeros_like(gray)
                count = 1
            else:
                count += 1
                delta = gray - mean
                mean += delta / count
                delta2 = gray - mean
                M2 += delta * delta2
        frame_idx += 1

    cap.release()

    if first_frame is None or width == 0 or height == 0:
        print("Warning: Layout detection failed due to missing frames or invalid dimensions.")
        return {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (height, width)}

    std_map = np.sqrt(M2 / (count - 1)) if count and count > 1 else np.zeros((small_height, small_width), dtype=np.float32)

    frame_gray = cv2.cvtColor(first_frame, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(frame_gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total_area = width * height
    rect_candidates = []
    scale_x = std_map.shape[1] / width if width else 1.0
    scale_y = std_map.shape[0] / height if height else 1.0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if total_area <= 0 or area < 0.02 * total_area:
            continue
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        x, y, w, h = cv2.boundingRect(approx)
        if w < 0.1 * width or h < 0.1 * height:
            continue
        if w * h > 0.95 * total_area:
            continue

        x0s = max(0, int(round(x * scale_x)))
        y0s = max(0, int(round(y * scale_y)))
        x1s = min(std_map.shape[1], int(round((x + w) * scale_x)))
        y1s = min(std_map.shape[0], int(round((y + h) * scale_y)))
        if x1s <= x0s or y1s <= y0s:
            continue
        region_std = float(std_map[y0s:y1s, x0s:x1s].mean())
        rect_candidates.append({
            "x": int(x),
            "y": int(y),
            "w": int(w),
            "h": int(h),
            "std_mean": region_std,
            "area": float(w * h)
        })

    layout = {"is_mixed": False, "ppt_rect": None, "video_rect": None, "frame_shape": (height, width)}

    # --- Step 1: 使用多帧 std_map 做一次全局 PPT 区域估计 ---
    if rect_candidates:
        margin_x = int(0.03 * width)
        margin_y = int(0.03 * height)
        ppt_like_candidates = []
        for r in rect_candidates:
            ar = r["w"] / float(r["h"]) if r["h"] > 0 else 0.0
            area_ratio = r["area"] / float(total_area) if total_area > 0 else 0.0

            touches_border = (
                r["x"] <= margin_x or
                r["y"] <= margin_y or
                (r["x"] + r["w"]) >= (width - margin_x) or
                (r["y"] + r["h"]) >= (height - margin_y)
            )

            # 经验阈值：
            # - 不贴边（屏幕通常不会贴满整个画面）
            # - 面积占比 20%–80%
            # - 长宽比在 [1.2, 2.2]，围绕 16:9≈1.78
            if (not touches_border and
                0.2 <= area_ratio <= 0.8 and
                1.2 <= ar <= 2.2):
                ppt_like_candidates.append({
                    **r,
                    "aspect_ratio": ar,
                    "area_ratio": area_ratio
                })

        if ppt_like_candidates:
            # 选择最像 16:9 且面积居中的候选
            def _ppt_score(rc):
                ar_diff = abs(rc["aspect_ratio"] - (16.0 / 9.0))
                area_diff = abs(rc["area_ratio"] - 0.5)
                motion = rc["std_mean"]  # PPT 区域通常比讲者区域更“静止”
                return ar_diff * 2.0 + area_diff + motion * 0.01

            best = min(ppt_like_candidates, key=_ppt_score)
            ppt_rect = _clamp_rect(best["x"], best["y"], best["w"], best["h"], width, height)

            # 粗略认为讲者区域在 PPT 左侧或右侧的一条竖条，仅用于调试打印
            if ppt_rect[0] > width // 2:
                # PPT 偏右，讲者在左
                video_rect = _clamp_rect(0, 0, ppt_rect[0], height, width, height)
            else:
                # PPT 偏左或居中，讲者在右
                video_rect = _clamp_rect(ppt_rect[0] + ppt_rect[2], 0, width - (ppt_rect[0] + ppt_rect[2]), height, width, height)

            layout.update({
                "is_mixed": True,
                "ppt_rect": ppt_rect,
                "video_rect": video_rect
            })
            print(f"Detected PPT screen by inner-rectangle heuristic: PPT rect={ppt_rect}, Video rect={video_rect}")
            return layout

    # --- Step 2: 回退到原来的 “两个大矩形 + 运动差异” 逻辑 ---
    if len(rect_candidates) >= 2:
        rect_candidates.sort(key=lambda r: r["area"], reverse=True)
        ppt = rect_candidates[0]
        remaining = rect_candidates[1:]
        video = max(remaining, key=lambda r: r["std_mean"]) if remaining else None

        ppt_rect = _clamp_rect(ppt["x"], ppt["y"], ppt["w"], ppt["h"], width, height)
        video_rect = _clamp_rect(video["x"], video["y"], video["w"], video["h"], width, height) if video else None

        layout.update({
            "is_mixed": video_rect is not None and video_rect != ppt_rect,
            "ppt_rect": ppt_rect,
            "video_rect": video_rect
        })
        if layout["is_mixed"]:
            print(f"Detected mixed layout (fallback): PPT rect={ppt_rect}, Video rect={video_rect}")
        else:
            print("Layout detection fallback found large rectangle but could not confirm mixed content. Using full frame.")
        return layout

    if std_map.size:
        mid_col = std_map.shape[1] // 2
        if mid_col > 0:
            left_std = float(std_map[:, :mid_col].mean())
            right_std = float(std_map[:, mid_col:].mean())
            max_std = max(left_std, right_std, 1e-6)
            std_diff_ratio = abs(left_std - right_std) / max_std
            if std_diff_ratio > 0.1:  # Heuristic threshold for motion imbalance
                if left_std <= right_std:
                    ppt_rect = _clamp_rect(0, 0, width // 2, height, width, height)
                    video_rect = _clamp_rect(width // 2, 0, width - width // 2, height, width, height)
                else:
                    ppt_rect = _clamp_rect(width // 2, 0, width - width // 2, height, width, height)
                    video_rect = _clamp_rect(0, 0, width // 2, height, width, height)
                layout.update({
                    "is_mixed": True,
                    "ppt_rect": ppt_rect,
                    "video_rect": video_rect
                })
                print(f"Heuristic split detected mixed layout: PPT rect={ppt_rect}, Video rect={video_rect}")
                return layout

    print("No mixed layout detected. Using full frame for extraction.")
    return layout

def _crop_to_ppt_region(frame: np.ndarray, layout_info=None) -> np.ndarray:
    """Crop a video frame to the active PPT region when one is known."""
    if layout_info and layout_info.get("ppt_rect"):
        x, y, w, h = layout_info["ppt_rect"]
        if w > 0 and h > 0:
            x2 = min(x + w, frame.shape[1])
            y2 = min(y + h, frame.shape[0])
            x = max(0, min(x, frame.shape[1] - 1))
            y = max(0, min(y, frame.shape[0] - 1))
            if x < x2 and y < y2:
                return frame[y:y2, x:x2].copy()
            print("Warning: PPT crop rectangle invalid. Falling back to full frame.")
        else:
            print("Warning: PPT crop rectangle has non-positive dimensions. Falling back to full frame.")
    return frame


def _resize_for_detection(frame: np.ndarray, detect_width: int) -> np.ndarray:
    """Convert a cropped frame to low-res grayscale for SSIM detection."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if detect_width <= 0 or gray.shape[1] <= detect_width:
        return gray
    detect_height = max(1, int(round(gray.shape[0] * (detect_width / gray.shape[1]))))
    return cv2.resize(gray, (detect_width, detect_height), interpolation=cv2.INTER_AREA)


def _image_extension(image_format: str) -> str:
    return "jpg" if image_format in {"jpg", "jpeg"} else image_format


def _image_filename(time_sec: float, image_format: str) -> str:
    timestamp_str_file = format_time(time_sec).replace(":", "_").replace(".", "_")
    return f"frame_{timestamp_str_file}.{_image_extension(image_format)}"


def _slide_info(output_dir: str, time_sec: float, image_format: str) -> dict:
    img_filename = _image_filename(time_sec, image_format)
    img_path = os.path.join(output_dir, "ppt_pics", img_filename)
    return {
        "time_sec": time_sec,
        "path": img_path,
        "filename": img_filename,
        "relative_path": os.path.join("ppt_pics", img_filename),
    }


def extract_detection_frames_ffmpeg(
    video_path,
    output_dir,
    interval_sec,
    layout_info,
    max_duration_sec: float = None,
    detect_width: int = 480,
    image_format: str = "png",
):
    """Fast low-res detection extraction through ffmpeg rawvideo pipe.

    This path is used only when the PPT crop is static. It lets ffmpeg sample,
    crop, scale, and gray-convert frames in C, avoiding Python-level frame
    skipping and OpenCV random/forward decode overhead.
    """
    if shutil.which("ffmpeg") is None:
        return None
    if not layout_info or not layout_info.get("ppt_rect"):
        return None
    if interval_sec <= 0 or detect_width <= 0:
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 0
    cap.release()

    if max_duration_sec is not None and max_duration_sec > 0:
        duration_sec = min(duration_sec, max_duration_sec)

    x, y, w, h = layout_info["ppt_rect"]
    if w <= 0 or h <= 0:
        return None

    detect_height = max(1, int(round(h * (detect_width / float(w)))))
    sample_fps = 1.0 / float(interval_sec)
    vf = f"fps=fps={sample_fps:.8f},crop={w}:{h}:{x}:{y},scale={detect_width}:{detect_height},format=gray"

    os.makedirs(output_dir, exist_ok=True)
    pics_dir = os.path.join(output_dir, "ppt_pics")
    os.makedirs(pics_dir, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
    ]
    if duration_sec > 0:
        cmd.extend(["-t", f"{duration_sec:.6f}"])
    cmd.extend(["-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"])

    print(
        f"Detecting slide changes with ffmpeg low-res pipe every {interval_sec} seconds "
        f"(width={detect_width}, crop={layout_info['ppt_rect']})..."
    )
    print(f"Video Info: FPS={fps:.2f}, Total Frames={total_frames}, Duration={format_time(duration_sec)}")

    frame_size = detect_width * detect_height
    detection_frames = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    frame_index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf:
                break
            if len(buf) != frame_size:
                print(f"Warning: ffmpeg returned a partial detection frame ({len(buf)}/{frame_size} bytes).")
                break
            current_time_sec = frame_index * interval_sec
            if duration_sec > 0 and current_time_sec > duration_sec + 1e-6:
                break
            info = _slide_info(output_dir, current_time_sec, image_format)
            detect_gray = np.frombuffer(buf, dtype=np.uint8).reshape((detect_height, detect_width)).copy()
            detection_frames.append(
                {
                    **info,
                    "detect_gray": detect_gray,
                }
            )
            frame_index += 1
            if frame_index % 100 == 0:
                print(f"  Prepared detection frame at {format_time(current_time_sec)}")
    finally:
        proc.stdout.close()

    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr is not None else ""
    return_code = proc.wait()
    if return_code != 0:
        print(f"Warning: ffmpeg detection pipe failed with code {return_code}. Falling back to OpenCV.")
        if stderr.strip():
            print(stderr.strip()[:2000])
        return None

    print(f"Finished preparing {len(detection_frames)} low-res detection frames via ffmpeg.")
    return detection_frames


def extract_detection_frames(
    video_path,
    output_dir,
    interval_sec,
    layout_info=None,
    dynamic_layout: bool = True,
    layout_refresh_interval: float = 10.0,
    max_duration_sec: float = None,
    detect_width: int = 480,
    image_format: str = "png",
):
    """Extract low-res in-memory frames for slide-change detection only.

    This is the fast first stage: it samples the configured timestamps, crops
    to the PPT region, downsizes to grayscale, and keeps the comparison image
    in memory instead of writing every sampled frame as PNG.
    """
    if not dynamic_layout:
        ffmpeg_frames = extract_detection_frames_ffmpeg(
            video_path,
            output_dir,
            interval_sec,
            layout_info,
            max_duration_sec=max_duration_sec,
            detect_width=detect_width,
            image_format=image_format,
        )
        if ffmpeg_frames is not None:
            return ffmpeg_frames

    print(
        f"Detecting slide changes from '{video_path}' every {interval_sec} seconds "
        f"using low-res width={detect_width}..."
    )

    os.makedirs(output_dir, exist_ok=True)
    pics_dir = os.path.join(output_dir, "ppt_pics")
    os.makedirs(pics_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 0

    if max_duration_sec is not None and max_duration_sec > 0:
        duration_sec = min(duration_sec, max_duration_sec)
        print(f"Limiting detection to first {max_duration_sec} seconds ({format_time(max_duration_sec)})")

    print(f"Video Info: FPS={fps:.2f}, Total Frames={total_frames}, Duration={format_time(duration_sec)}")

    sample_times: List[float] = []
    current_time_sec = 0.0
    while current_time_sec <= duration_sec:
        sample_times.append(current_time_sec)
        current_time_sec += interval_sec
        if interval_sec <= 0:
            print("Error: Interval must be positive.")
            break
        if len(sample_times) > (duration_sec / interval_sec) * 2 and duration_sec > 0:
            print("Warning: Prepared significantly more frame timestamps than expected. Stopping.")
            break

    detection_frames = []
    current_layout = layout_info
    last_layout_update_time = -1e9
    current_frame_id = 0

    for current_time_sec in sample_times:
        target_frame_id = int(current_time_sec * fps)

        if target_frame_id < current_frame_id:
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame_id)
            current_frame_id = target_frame_id

        while current_frame_id < target_frame_id:
            if not cap.grab():
                break
            current_frame_id += 1

        ret, frame = cap.read()
        current_frame_id += 1

        if not ret:
            print(f"Warning: Could not read frame at {format_time(current_time_sec)}")
            continue

        if dynamic_layout and (current_layout is None or current_time_sec - last_layout_update_time >= layout_refresh_interval):
            current_layout = _detect_ppt_rect_in_single_frame(frame)
            last_layout_update_time = current_time_sec
            print(f"[Dynamic layout] t={format_time(current_time_sec)} PPT rect={current_layout.get('ppt_rect')}")

        active_layout = current_layout if dynamic_layout else layout_info
        cropped = _crop_to_ppt_region(frame, active_layout)
        detect_gray = _resize_for_detection(cropped, detect_width)

        info = _slide_info(output_dir, current_time_sec, image_format)
        detection_frames.append(
            {
                **info,
                "detect_gray": detect_gray,
            }
        )

        if len(detection_frames) % 100 == 0:
            print(f"  Prepared detection frame at {format_time(current_time_sec)}")

    cap.release()
    print(f"Finished preparing {len(detection_frames)} low-res detection frames.")
    return detection_frames


def _video_duration(video_path: str, max_duration_sec: float = None) -> float:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return max_duration_sec or 0.0
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    duration_sec = total_frames / fps if fps > 0 else 0.0
    if max_duration_sec is not None and max_duration_sec > 0:
        return min(duration_sec, max_duration_sec)
    return duration_sec


def extract_keyframe_detection_frames_ffmpeg(
    video_path,
    output_dir,
    layout_info,
    max_duration_sec: float = None,
    detect_width: int = 240,
    image_format: str = "png",
):
    """Read low-res keyframes and their timestamps in one ffmpeg pass."""
    if shutil.which("ffmpeg") is None:
        return None
    if not layout_info or not layout_info.get("ppt_rect"):
        return None
    if detect_width <= 0:
        return None

    x, y, w, h = layout_info["ppt_rect"]
    if w <= 0 or h <= 0:
        return None

    detect_height = max(1, int(round(h * (detect_width / float(w)))))
    frame_size = detect_width * detect_height
    vf = f"crop={w}:{h}:{x}:{y},scale={detect_width}:{detect_height},format=gray,showinfo"

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-skip_frame",
        "nokey",
        "-i",
        video_path,
    ]
    if max_duration_sec is not None and max_duration_sec > 0:
        cmd.extend(["-t", f"{max_duration_sec:.6f}"])
    cmd.extend(["-an", "-sn", "-vf", vf, "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "gray", "-"])

    print(
        f"Detecting keyframe slide candidates with ffmpeg keyframe scan "
        f"(width={detect_width}, crop={layout_info['ppt_rect']})..."
    )

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "ppt_pics"), exist_ok=True)
    showinfo_re = re.compile(r"n:\s*(\d+).*?pts_time:([0-9.]+|N/A)")
    frames = []

    with tempfile.NamedTemporaryFile("wb", prefix="keyframe-showinfo-", suffix=".log", delete=False) as stderr_file:
        stderr_path = stderr_file.name
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file)
        assert proc.stdout is not None
        try:
            while True:
                buf = proc.stdout.read(frame_size)
                if not buf:
                    break
                if len(buf) != frame_size:
                    print(f"Warning: ffmpeg returned a partial keyframe ({len(buf)}/{frame_size} bytes).")
                    break
                frames.append(np.frombuffer(buf, dtype=np.uint8).reshape((detect_height, detect_width)).copy())
        finally:
            proc.stdout.close()
        return_code = proc.wait()

    stderr = ""
    try:
        with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
            stderr = f.read()
    finally:
        try:
            os.unlink(stderr_path)
        except OSError:
            pass

    if return_code != 0:
        print(f"Warning: ffmpeg keyframe scan failed with code {return_code}.")
        if stderr.strip():
            print(stderr.strip()[-2000:])
        return None

    indexed_times = {}
    for match in showinfo_re.finditer(stderr):
        index = int(match.group(1))
        value = match.group(2)
        if value != "N/A":
            indexed_times[index] = float(value)

    detection_frames = []
    for index, detect_gray in enumerate(frames):
        if index not in indexed_times:
            continue
        time_sec = indexed_times[index]
        info = _slide_info(output_dir, time_sec, image_format)
        detection_frames.append({**info, "detect_gray": detect_gray})

    print(f"Finished preparing {len(detection_frames)} low-res keyframes via ffmpeg.")
    return detection_frames


def _cluster_times(times: List[float], gap_sec: float = 12.0) -> List[List[float]]:
    clusters: List[List[float]] = []
    for time_sec in sorted(times):
        if not clusters or time_sec - clusters[-1][-1] > gap_sec:
            clusters.append([time_sec])
        else:
            clusters[-1].append(time_sec)
    return clusters


def _snap_to_interval(time_sec: float, interval_sec: float, duration_sec: float) -> float:
    snapped = round(time_sec / interval_sec) * interval_sec
    return max(0.0, min(duration_sec, snapped))


def _merge_windows(windows: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not windows:
        return []
    windows = sorted(windows)
    merged: List[List[float]] = []
    for start, end in windows:
        if not merged or start > merged[-1][1] + 1e-6:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def extract_detection_window_ffmpeg(
    video_path,
    window: Tuple[float, float],
    interval_sec: float,
    layout_info,
    detect_width: int,
):
    start, end = window
    if end <= start:
        return []
    x, y, w, h = layout_info["ppt_rect"]
    detect_height = max(1, int(round(h * (detect_width / float(w)))))
    frame_size = detect_width * detect_height
    vf = f"fps=fps={1.0 / interval_sec:.8f},crop={w}:{h}:{x}:{y},scale={detect_width}:{detect_height},format=gray"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{(end - start):.6f}",
        "-i",
        video_path,
        "-an",
        "-sn",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]

    frames = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf:
                break
            if len(buf) != frame_size:
                print(f"Warning: ffmpeg returned a partial local detection frame ({len(buf)}/{frame_size} bytes).")
                break
            frames.append(
                {
                    "time_sec": start + index * interval_sec,
                    "detect_gray": np.frombuffer(buf, dtype=np.uint8).reshape((detect_height, detect_width)).copy(),
                }
            )
            index += 1
    finally:
        proc.stdout.close()

    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr is not None else ""
    return_code = proc.wait()
    if return_code != 0:
        print(f"Warning: ffmpeg local refine window failed with code {return_code}: {window}")
        if stderr.strip():
            print(stderr.strip()[-1000:])
        return []
    return frames


def hybrid_keyframe_slide_changes(
    video_path,
    output_dir,
    interval_sec,
    threshold,
    layout_info,
    max_duration_sec: float = None,
    detect_width: int = 240,
    image_format: str = "png",
    refine_workers: int = 6,
):
    """Fast default: keyframe recall plus local accurate refinement."""
    if interval_sec <= 0:
        return None
    if not layout_info or not layout_info.get("ppt_rect"):
        return None

    duration_sec = _video_duration(video_path, max_duration_sec=max_duration_sec)
    if duration_sec <= 0:
        return None

    keyframes = extract_keyframe_detection_frames_ffmpeg(
        video_path,
        output_dir,
        layout_info,
        max_duration_sec=max_duration_sec,
        detect_width=detect_width,
        image_format=image_format,
    )
    if not keyframes or len(keyframes) < 2:
        return None

    keyframe_changes = find_slide_changes(keyframes, threshold=threshold)
    candidate_times = sorted({float(item["time_sec"]) for item in keyframe_changes})
    clusters = _cluster_times(candidate_times)

    accepted_times = {0.0}
    windows: List[Tuple[float, float]] = []
    for cluster in clusters:
        if len(cluster) == 1:
            accepted_times.add(_snap_to_interval(cluster[0], interval_sec, duration_sec))
            continue
        start = max(0.0, np.floor((cluster[0] - 2 * interval_sec) / interval_sec) * interval_sec)
        end = min(duration_sec, np.ceil((cluster[-1] + 3 * interval_sec) / interval_sec) * interval_sec)
        windows.append((float(start), float(end)))

    # Validate the tail; screen recordings often end with a final state change.
    tail_start = max(0.0, np.floor((duration_sec - 4 * interval_sec) / interval_sec) * interval_sec)
    windows.append((float(tail_start), duration_sec))
    windows = _merge_windows(windows)

    print(
        f"Hybrid keyframe mode: {len(candidate_times)} keyframe candidates, "
        f"{len(windows)} local refine windows."
    )

    frames_by_time: Dict[float, np.ndarray] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, refine_workers)) as executor:
        futures = [
            executor.submit(
                extract_detection_window_ffmpeg,
                video_path,
                window,
                interval_sec,
                layout_info,
                detect_width,
            )
            for window in windows
        ]
        for future in concurrent.futures.as_completed(futures):
            for frame in future.result():
                frames_by_time[round(float(frame["time_sec"]), 3)] = frame["detect_gray"]

    local_times = sorted(frames_by_time)
    for prev_t, curr_t in zip(local_times, local_times[1:]):
        if abs((curr_t - prev_t) - interval_sec) > 1e-3:
            continue
        prev = frames_by_time[prev_t]
        curr = frames_by_time[curr_t]
        data_range = max(int(prev.max()) - int(prev.min()), 1)
        if ssim(prev, curr, data_range=data_range) < threshold:
            accepted_times.add(curr_t)

    slide_changes = []
    for time_sec in sorted(t for t in accepted_times if 0 <= t <= duration_sec + 1e-6):
        slide_changes.append(_slide_info(output_dir, time_sec, image_format))

    print(f"Hybrid keyframe mode found {len(slide_changes)} refined slide starts.")
    return slide_changes


def materialize_slide_frames(
    video_path,
    slide_changes,
    layout_info=None,
    dynamic_layout: bool = True,
    image_format: str = "png",
    jpeg_quality: int = 85,
    workers: int = 6,
):
    """Save high-resolution images only for frames identified as slide starts."""
    print(f"Materializing {len(slide_changes)} high-resolution slide frames as {image_format}...")
    if not slide_changes:
        return []

    def _write_slide(slide):
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Error: Could not open video file {video_path}")
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_id = int(slide["time_sec"] * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print(f"Warning: Could not read selected slide frame at {format_time(slide['time_sec'])}")
            return None

        active_layout = _detect_ppt_rect_in_single_frame(frame) if dynamic_layout else layout_info
        frame_to_save = _crop_to_ppt_region(frame, active_layout)
        os.makedirs(os.path.dirname(slide["path"]), exist_ok=True)
        write_args = []
        if image_format in {"jpg", "jpeg"}:
            write_args = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
        elif image_format == "webp":
            write_args = [int(cv2.IMWRITE_WEBP_QUALITY), int(jpeg_quality)]
        cv2.imwrite(slide["path"], frame_to_save, write_args)
        slide.pop("detect_gray", None)
        return slide

    saved = []
    if dynamic_layout or workers <= 1:
        for index, slide in enumerate(slide_changes, start=1):
            written = _write_slide(slide)
            if written:
                saved.append(written)
            if index % 20 == 0:
                print(f"  Materialized {index}/{len(slide_changes)} slide frames...")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_write_slide, slide) for slide in slide_changes]
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                written = future.result()
                if written:
                    saved.append(written)
                if index % 20 == 0:
                    print(f"  Materialized {index}/{len(slide_changes)} slide frames...")
        saved.sort(key=lambda item: item["time_sec"])

    print(f"Finished materializing {len(saved)} slide frames.")
    return saved


def _gray_for_comparison(frame_info):
    if "detect_gray" in frame_info:
        return frame_info["detect_gray"]

    img = cv2.imread(frame_info["path"])
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _make_frame_info(output_dir: str, current_time_sec: float) -> Dict[str, object]:
    timestamp_str_file = format_time(current_time_sec).replace(":", "_").replace(".", "_")
    img_filename = f"frame_{timestamp_str_file}.png"
    img_path = os.path.join(output_dir, "ppt_pics", img_filename)
    relative_path = os.path.join("ppt_pics", img_filename)
    return {
        "time_sec": current_time_sec,
        "path": img_path,
        "filename": img_filename,
        "relative_path": relative_path,
    }


def _crop_to_ppt_region(frame: np.ndarray, active_layout=None) -> np.ndarray:
    if not active_layout or not active_layout.get("ppt_rect"):
        return frame

    x, y, w, h = active_layout["ppt_rect"]
    if w <= 0 or h <= 0:
        print("Warning: PPT crop rectangle has non-positive dimensions. Falling back to full frame.")
        return frame

    x2 = min(x + w, frame.shape[1])
    y2 = min(y + h, frame.shape[0])
    x = max(0, min(x, frame.shape[1] - 1))
    y = max(0, min(y, frame.shape[0] - 1))
    if x < x2 and y < y2:
        return frame[y:y2, x:x2].copy()

    print("Warning: PPT crop rectangle invalid. Falling back to full frame.")
    return frame


def _prepare_gray_for_streaming_ssim(
    frame: np.ndarray,
    target_size: Optional[Tuple[int, int]],
) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if target_size is not None and (gray.shape[1], gray.shape[0]) != target_size:
        gray = cv2.resize(gray, target_size, interpolation=cv2.INTER_AREA)
    return gray


def _streaming_ssim_changed(prev_gray: np.ndarray, curr_gray: np.ndarray, threshold: float) -> bool:
    if prev_gray.shape != curr_gray.shape:
        print("Warning: Streaming SSIM frame dimensions mismatch. Treating current frame as a change.")
        return True
    similarity_index = ssim(prev_gray, curr_gray, data_range=255)
    return similarity_index < threshold


def _resize_frame_max_side(frame: np.ndarray, max_side: int) -> np.ndarray:
    if max_side <= 0:
        return frame
    height, width = frame.shape[:2]
    largest_side = max(height, width)
    if largest_side <= max_side:
        return frame
    scale = max_side / float(largest_side)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    return cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)


def extract_slide_changes_streaming(
    video_path,
    output_dir,
    interval_sec,
    threshold,
    layout_info=None,
    dynamic_layout: bool = True,
    layout_refresh_interval: float = 10.0,
    max_duration_sec: float = None,
    ssim_max_side: int = 512,
    output_max_side: int = 0,
):
    """Extract only slide-change frames by comparing sampled frames in memory."""
    print(
        f"Streaming slide-change extraction from '{video_path}' every {interval_sec} seconds "
        f"with SSIM threshold < {threshold}..."
    )

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created directory: {output_dir}")

    pics_dir = os.path.join(output_dir, "ppt_pics")
    if not os.path.exists(pics_dir):
        os.makedirs(pics_dir)
        print(f"Created images directory: {pics_dir}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 0
    if max_duration_sec is not None and max_duration_sec > 0:
        duration_sec = min(duration_sec, max_duration_sec)
        print(f"Limiting extraction to first {max_duration_sec} seconds ({format_time(max_duration_sec)})")

    print(f"Video Info: FPS={fps:.2f}, Total Frames={total_frames}, Duration={format_time(duration_sec)}")

    slide_changes = []
    sampled_count = 0
    saved_count = 0
    current_layout = layout_info
    last_layout_update_time = -1e9
    prev_gray = None
    target_size: Optional[Tuple[int, int]] = None
    sample_stride_frames = max(1, int(round(fps * interval_sec))) if fps > 0 else 1
    max_frame_id = int(duration_sec * fps) if fps > 0 else 0
    frame_idx = 0

    while frame_idx <= max_frame_id:
        grabbed = cap.grab()
        if not grabbed:
            break
        if frame_idx % sample_stride_frames != 0:
            frame_idx += 1
            continue

        current_time_sec = frame_idx / fps if fps > 0 else sampled_count * interval_sec
        ret, frame = cap.retrieve()
        if not ret:
            print(f"Warning: Could not read frame at {format_time(current_time_sec)}")
            frame_idx += 1
            continue

        if dynamic_layout and (current_layout is None or current_time_sec - last_layout_update_time >= layout_refresh_interval):
            current_layout = _detect_ppt_rect_in_single_frame(frame)
            last_layout_update_time = current_time_sec
            print(f"[Dynamic layout] t={format_time(current_time_sec)} PPT rect={current_layout.get('ppt_rect')}")

        active_layout = current_layout if dynamic_layout else layout_info
        frame_to_save = _crop_to_ppt_region(frame, active_layout)

        if target_size is None:
            gray_for_size = cv2.cvtColor(frame_to_save, cv2.COLOR_BGR2GRAY)
            target_size = _target_ssim_size(gray_for_size.shape, ssim_max_side)
            curr_gray = cv2.resize(gray_for_size, target_size, interpolation=cv2.INTER_AREA) if target_size is not None else gray_for_size
        else:
            curr_gray = _prepare_gray_for_streaming_ssim(frame_to_save, target_size)

        sampled_count += 1
        is_change = prev_gray is None or _streaming_ssim_changed(prev_gray, curr_gray, threshold)
        if is_change:
            frame_info = _make_frame_info(output_dir, current_time_sec)
            frame_to_write = _resize_frame_max_side(frame_to_save, output_max_side)
            cv2.imwrite(str(frame_info["path"]), frame_to_write)
            slide_changes.append(frame_info)
            saved_count += 1

        prev_gray = curr_gray

        if sampled_count % 20 == 0:
            print(f"  Sampled {sampled_count} frames; saved {saved_count} slide-change frames at {format_time(current_time_sec)}")

        frame_idx += 1
        if interval_sec <= 0:
             print("Error: Interval must be positive.")
             break
        if sampled_count > (duration_sec / interval_sec) * 2 and duration_sec > 0:
            print("Warning: Sampled significantly more frames than expected. Stopping.")
            break

    cap.release()
    print(f"Streaming extraction sampled {sampled_count} frames and saved {saved_count} slide-change frames.")
    return slide_changes


def _select_torch_ssim_device(backend: str):
    """Return the best torch device for batch SSIM, or None if unavailable."""
    if torch is None or torch_ssim is None:
        return None

    mps_backend = getattr(torch.backends, "mps", None)
    if backend == "mps":
        if mps_backend is not None and mps_backend.is_available():
            return torch.device("mps")
        return None

    if mps_backend is not None and mps_backend.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _target_ssim_size(gray_shape: Tuple[int, int], max_side: int) -> Optional[Tuple[int, int]]:
    """Return OpenCV resize target (width, height), preserving aspect ratio."""
    if max_side <= 0:
        return None
    height, width = gray_shape
    largest_side = max(height, width)
    if largest_side <= 0:
        return None
    scale = min(1.0, max_side / float(largest_side))
    target_width = max(11, int(round(width * scale)))
    target_height = max(11, int(round(height * scale)))
    return target_width, target_height


def _report_slide_changes(frame_list, slide_changes_indices):
    """Build slide-change records and print the existing change report."""
    sorted_indices = sorted(list(slide_changes_indices))
    slide_changes = [frame_list[i] for i in sorted_indices]

    print("Change detection report:")
    if len(sorted_indices) > 1:
        for k in range(1, len(sorted_indices)):
             prev_idx = sorted_indices[k-1]
             curr_idx = sorted_indices[k]
             print(f"  Slide change identified: Frame {prev_idx} ({frame_list[prev_idx]['filename']}) -> Frame {curr_idx} ({frame_list[curr_idx]['filename']})")
    elif len(sorted_indices) == 1:
         print("  No significant changes detected after the first frame.")
    else: # Should not happen due to initialization with {0}
         print("  No frames identified.")

    print(f"Found {len(slide_changes)} potential slide starts (including first frame).")
    return slide_changes


def _find_slide_changes_mps(frame_list, threshold=0.9, max_side=512, backend="auto"):
    """Batch adjacent-frame SSIM with torch on Apple MPS when available."""
    device = _select_torch_ssim_device(backend)
    if device is None:
        raise RuntimeError("torch/pytorch-msssim or requested torch device is unavailable")

    print(f"Comparing {len(frame_list)} frames with batched SSIM threshold < {threshold}...")
    if not frame_list or len(frame_list) < 2:
        print("Not enough frames to compare.")
        return frame_list

    gray_frames: List[Optional[np.ndarray]] = []
    first_valid_shape: Optional[Tuple[int, int]] = None
    for frame_info in frame_list:
        img = cv2.imread(frame_info["path"])
        if img is None:
            print(f"Warning: Could not read image for comparison: {frame_info['path']}")
            gray_frames.append(None)
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if first_valid_shape is None:
            first_valid_shape = gray.shape
        gray_frames.append(gray)

    if first_valid_shape is None:
        print("No readable frames to compare.")
        return _report_slide_changes(frame_list, {0})

    target_size = _target_ssim_size(first_valid_shape, max_side)
    if target_size is not None:
        resized_frames: List[Optional[np.ndarray]] = []
        for gray in gray_frames:
            if gray is None:
                resized_frames.append(None)
            elif (gray.shape[1], gray.shape[0]) != target_size:
                resized_frames.append(cv2.resize(gray, target_size, interpolation=cv2.INTER_AREA))
            else:
                resized_frames.append(gray)
        gray_frames = resized_frames

    valid_pairs = []
    for i in range(1, len(gray_frames)):
        prev_gray = gray_frames[i - 1]
        curr_gray = gray_frames[i]
        if prev_gray is None or curr_gray is None:
            continue
        if prev_gray.shape != curr_gray.shape:
            print(f"Warning: Frame dimensions mismatch between {frame_list[i - 1]['path']} and {frame_list[i]['path']}. Skipping comparison.")
            continue
        valid_pairs.append((i, prev_gray, curr_gray))

    if not valid_pairs:
        print("No valid adjacent frame pairs to compare.")
        return _report_slide_changes(frame_list, {0})

    sample_height, sample_width = valid_pairs[0][1].shape
    bytes_per_pair = max(1, 2 * sample_height * sample_width * 4)
    chunk_size = max(1, min(64, (256 * 1024 * 1024) // bytes_per_pair))
    max_side_label = "original" if max_side <= 0 else str(max_side)
    print(f"[SSIM] backend={device.type} device={device} max_side={max_side_label} chunk_size={chunk_size} pairs={len(valid_pairs)}")

    slide_changes_indices = {0}
    processed_count = 0

    with torch.no_grad():
        start = 0
        while start < len(valid_pairs):
            chunk_shape = valid_pairs[start][1].shape
            end = start
            while end < len(valid_pairs) and end - start < chunk_size and valid_pairs[end][1].shape == chunk_shape:
                end += 1

            chunk = valid_pairs[start:end]
            prev_batch = np.stack([pair[1] for pair in chunk]).astype(np.float32) / 255.0
            curr_batch = np.stack([pair[2] for pair in chunk]).astype(np.float32) / 255.0
            prev_tensor = torch.from_numpy(prev_batch).unsqueeze(1).to(device)
            curr_tensor = torch.from_numpy(curr_batch).unsqueeze(1).to(device)

            scores = torch_ssim(prev_tensor, curr_tensor, data_range=1.0, size_average=False)
            scores_np = scores.detach().cpu().numpy()
            for (frame_index, _, _), score in zip(chunk, scores_np):
                if float(score) < threshold:
                    slide_changes_indices.add(frame_index)

            processed_count += len(chunk)
            if processed_count % 50 == 0 or processed_count == len(valid_pairs):
                print(f"  Completed {processed_count}/{len(valid_pairs)} comparison tasks...")
            start = end

    return _report_slide_changes(frame_list, slide_changes_indices)


def compare_frame_pair(prev_frame_info, current_frame_info, threshold, index):
    """Helper function to compare a single pair of frames for SSIM."""
    try:
        prev_img_gray = _gray_for_comparison(prev_frame_info)
        curr_img_gray = _gray_for_comparison(current_frame_info)

        if prev_img_gray is None or curr_img_gray is None:
            print(f"Warning: Could not read image for comparison: {prev_frame_info['path'] if prev_img_gray is None else ''} {current_frame_info['path'] if curr_img_gray is None else ''}")
            return None # Cannot compare if images are missing

        if prev_img_gray.shape != curr_img_gray.shape:
            print(f"Warning: Frame dimensions mismatch between {prev_frame_info['path']} and {current_frame_info['path']}. Skipping comparison.")
            # Consider this a change? Or skip? Let's skip for now.
            # If skipped, this frame won't be added unless the *next* comparison detects a change.
            return None

        # Calculate SSIM
        data_range = max(int(prev_img_gray.max()) - int(prev_img_gray.min()), 1)
        similarity_index = ssim(prev_img_gray, curr_img_gray, data_range=data_range)

        # print(f"  Compared {prev_frame_info['filename']} and {current_frame_info['filename']} (Index {index}): SSIM = {similarity_index:.4f}")

        if similarity_index < threshold:
            # print(f"  Change detected at index {index} (SSIM: {similarity_index:.4f})")
            return index # Return the index of the *current* frame where the change was detected
        return None
    except Exception as e:
        print(f"Error comparing frames {prev_frame_info['path']} and {current_frame_info['path']} (Index {index}): {e}")
        return None # Return None on error

def _find_slide_changes_cpu(frame_list, threshold=0.9):
    """Compares adjacent frames in parallel to find significant changes."""
    print(f"Comparing {len(frame_list)} frames in parallel with SSIM threshold < {threshold}...")
    if not frame_list or len(frame_list) < 2:
        print("Not enough frames to compare.")
        return frame_list

    slide_changes_indices = {0} # Use a set for unique indices, always include the first frame (index 0)

    # Prepare tasks for the thread pool
    tasks = []
    # max_workers=None uses a sensible default, often based on CPU cores
    with concurrent.futures.ThreadPoolExecutor() as executor:
        for i in range(1, len(frame_list)):
            prev_frame_info = frame_list[i-1]
            current_frame_info = frame_list[i]
            # Submit the comparison task to the thread pool
            future = executor.submit(compare_frame_pair, prev_frame_info, current_frame_info, threshold, i)
            tasks.append(future)

        # Collect results as they complete
        print("Waiting for comparison tasks to complete...")
        processed_count = 0
        for future in concurrent.futures.as_completed(tasks):
            result_index = future.result()
            if result_index is not None:
                slide_changes_indices.add(result_index)
            processed_count += 1
            if processed_count % 50 == 0: # Print progress occasionally
                 print(f"  Completed {processed_count}/{len(tasks)} comparison tasks...")


    return _report_slide_changes(frame_list, slide_changes_indices)


def find_slide_changes(frame_list, threshold=0.9, backend="auto", max_side=512):
    """Find slide changes with an accelerated torch backend when available."""
    backend = (backend or "auto").lower()
    if backend not in {"auto", "mps", "cpu"}:
        print(f"Warning: Unknown SSIM backend '{backend}'. Falling back to auto.")
        backend = "auto"

    if backend in {"auto", "mps"}:
        try:
            return _find_slide_changes_mps(
                frame_list,
                threshold=threshold,
                max_side=max_side,
                backend=backend,
            )
        except Exception as e:
            print(f"[SSIM] MPS path failed ({e}); falling back to CPU.")

    return _find_slide_changes_cpu(frame_list, threshold)

def generate_markdown(slide_changes, output_dir, md_filename="ppt_timestamps.md"):
    """Generates a Markdown file listing slide changes with timestamps and images."""
    md_path = os.path.join(output_dir, md_filename)
    print(f"Generating Markdown report: {md_path}")

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("# Presentation Slide Timestamps\n\n")

        if not slide_changes:
            f.write("No slide changes detected.\n")
            return

        for i, slide in enumerate(slide_changes):
            timestamp_formatted = format_time(slide['time_sec'])
            # Use relative path for portability
            relative_img_path = slide['relative_path'] # Image is in the ppt_pics subdirectory

            f.write(f"## Slide {i+1} (Timestamp: {timestamp_formatted})\n\n")
            f.write(f"![Slide {i+1} at {timestamp_formatted}]({relative_img_path})\n\n")
            f.write("---\n\n")

    print("Markdown report generated successfully.")


def get_default_output_dir(video_file):
    """Generate default output directory name based on input file and current timestamp."""
    # Get the base name without extension
    base_name = os.path.splitext(os.path.basename(video_file))[0]

    # Get current timestamp in format YYYYMMDDHHMMSS
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    # Combine base name and timestamp
    return f"{base_name}_{timestamp}"

def main():
    parser = argparse.ArgumentParser(description="Extract timestamps of slide changes from a presentation video.")
    parser.add_argument("video_file", help="Path to the input video file.")
    parser.add_argument("-o", "--output_dir", help="Directory to save extracted frames and markdown file (default: input_filename_YYYYMMDDHHMMSS).")
    parser.add_argument("-i", "--interval", type=float, default=2.0, help="Interval in seconds between frame captures (default: 2.0).")
    parser.add_argument("-t", "--threshold", type=float, default=0.9, help="SSIM threshold for detecting changes (lower means more sensitive, default: 0.9).")
    parser.add_argument(
        "--ssim_backend",
        choices=("auto", "mps", "cpu"),
        default="auto",
        help="SSIM comparison backend: auto tries Apple MPS acceleration when available, mps requires Apple MPS, cpu uses the legacy skimage path (default: auto).",
    )
    parser.add_argument(
        "--ssim_max_side",
        type=int,
        default=512,
        help="Resize frames so their longest side is at most this many pixels before accelerated SSIM; set 0 to use original resolution (default: 512).",
    )
    parser.add_argument(
        "--output_max_side",
        type=int,
        default=0,
        help="Streaming mode: resize saved slide screenshots so their longest side is at most this many pixels; set 0 to keep original resolution (default: 0).",
    )
    parser.add_argument("--md_name", help="Name for the output markdown file (default: same as output directory name with .md extension).")
    parser.add_argument(
        "--detect-width",
        type=int,
        default=240,
        help="Low-res grayscale width used for slide-change detection (default: 240).",
    )
    parser.add_argument(
        "--detection-backend",
        choices=["hybrid-keyframe", "accurate"],
        default="hybrid-keyframe",
        help="Slide detection backend. hybrid-keyframe is the fast default; accurate scans the full video at --interval.",
    )
    parser.add_argument(
        "--image-format",
        choices=["jpg", "jpeg", "png", "webp"],
        default="png",
        help="Image format for materialized slide frames (default: png).",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=85,
        help="JPEG/WebP quality when --image-format is jpg/jpeg/webp (default: 85).",
    )
    parser.add_argument(
        "--materialize-workers",
        type=int,
        default=6,
        help="Parallel workers for writing selected slide images when layout is static (default: 6).",
    )
    parser.add_argument(
        "--ppt_rect",
        help="Manual PPT crop rect in relative coordinates 'x1,y1,x2,y2' (each in [0,1]). "
             "If set, this rect will be used for all frames and dynamic detection is disabled.",
    )
    parser.add_argument(
        "-I",
        "--interactive",
        action="store_true",
        help="Open an interactive window to let the user draw the PPT region with the mouse "
             "before processing. The chosen rectangle is saved next to the video as "
             "'<video>.ppt_rect.json' (or --rect_file) for reuse.",
    )
    parser.add_argument(
        "--rect_file",
        help="Path to a JSON sidecar file storing the PPT rect. Defaults to "
             "'<video>.ppt_rect.json' next to the input video. Loaded automatically when present; "
             "written when --interactive selects a new rect.",
    )
    parser.add_argument(
        "--force_reselect",
        action="store_true",
        help="When used with --interactive, ignore the existing rect file and force redrawing.",
    )
    parser.add_argument(
        "--max_duration",
        type=float,
        help="Maximum duration in seconds to process (e.g., 180 for 3 minutes). If not set, processes entire video.",
    )
    parser.add_argument(
        "--full-frame",
        action="store_true",
        help="Use the entire video frame for screenshots and SSIM (no PPT region crop, no mixed-layout heuristics, no dynamic per-frame crop).",
    )
    parser.add_argument(
        "--legacy_extract_all",
        action="store_true",
        help="Use the old two-pass flow: save every sampled frame, then compare saved PNGs. By default, only slide-change frames are saved.",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.video_file):
        print(f"Error: Input video file not found: {args.video_file}")
        return

    if args.interval <= 0:
        print("Error: Frame extraction interval must be positive.")
        return

    if not (0 < args.threshold <= 1.0):
        print("Error: SSIM threshold must be between 0 and 1.")
        return

    if args.ssim_max_side < 0:
        print("Error: --ssim_max_side must be 0 or a positive integer.")
        return

    if args.output_max_side < 0:
        print("Error: --output_max_side must be 0 or a positive integer.")
        return

    # Set default output directory if not specified
    if not args.output_dir:
        args.output_dir = get_default_output_dir(args.video_file)
        print(f"No output directory specified. Using default: {args.output_dir}")

    # Set default markdown filename if not specified
    # Use the output directory name as the markdown filename
    if not args.md_name:
        # Get just the directory name without the full path
        dir_name = os.path.basename(args.output_dir)
        args.md_name = f"{dir_name}.md"
        print(f"No markdown filename specified. Using default: {args.md_name}")

    manual_rect: Optional[Tuple[float, float, float, float]] = None
    if args.full_frame:
        if args.ppt_rect:
            print("Warning: --full-frame ignores --ppt_rect.")
        if args.interactive:
            print("Note: --interactive is ignored when --full-frame is set.")
    elif args.ppt_rect:
        try:
            parts = [float(p.strip()) for p in args.ppt_rect.split(",")]
            if len(parts) != 4:
                raise ValueError
            manual_rect = tuple(parts)  # type: ignore[assignment]
        except ValueError:
            print("Error: --ppt_rect must be four comma-separated floats like '0.05,0.08,0.75,0.92'.")
            return

    # Sidecar file path for persisted interactive rect
    rect_file = args.rect_file or _default_rect_file(args.video_file)

    # If no explicit --ppt_rect, try loading from sidecar file (unless forced to reselect)
    if not args.full_frame and manual_rect is None and not args.force_reselect:
        loaded = load_rect_file(rect_file)
        if loaded is not None:
            manual_rect = loaded
            print(f"Loaded PPT rect from sidecar file: {rect_file} -> {manual_rect}")

    # Interactive mode: let the user draw the rect with the mouse
    if not args.full_frame and args.interactive:
        picked = interactive_select_ppt_rect(
            args.video_file,
            initial_rect_relative=manual_rect,
        )
        if picked is None:
            if manual_rect is None:
                print("Interactive selection cancelled and no rect available. Aborting.")
                return
            print("Interactive selection cancelled, falling back to previously known rect.")
        else:
            manual_rect = picked
            # Also compute absolute rect for the record
            cap = cv2.VideoCapture(args.video_file)
            w_full = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if cap.isOpened() else 0
            h_full = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if cap.isOpened() else 0
            cap.release()
            rect_abs = None
            if w_full and h_full:
                x1 = int(round(picked[0] * w_full))
                y1 = int(round(picked[1] * h_full))
                x2 = int(round(picked[2] * w_full))
                y2 = int(round(picked[3] * h_full))
                rect_abs = (x1, y1, x2 - x1, y2 - y1)
            save_rect_file(
                rect_file,
                args.video_file,
                picked,
                frame_shape=(h_full, w_full),
                rect_absolute=rect_abs,
            )

    if args.full_frame:
        layout_info = full_frame_layout(args.video_file)
        use_dynamic = False
    else:
        layout_info = detect_mixed_layout(args.video_file, manual_rect=manual_rect)
        if layout_info.get("ppt_rect"):
            print(f"Using PPT crop rect: {layout_info.get('ppt_rect')} (is_mixed={layout_info.get('is_mixed')})")
        # 如果用户指定了手动矩形，就不再做动态检测，以避免尺寸变化导致的抖动
        use_dynamic = manual_rect is None

    if args.legacy_extract_all:
        # 1. Extract every sampled frame, then compare saved PNGs.
        extracted_frames = extract_frames(
            args.video_file,
            args.output_dir,
            args.interval,
            layout_info=layout_info,
            dynamic_layout=use_dynamic,
            max_duration_sec=args.max_duration,
        )

        # 2. Find slide changes
        if extracted_frames:
            slide_changes = find_slide_changes(
                extracted_frames,
                args.threshold,
                backend=args.ssim_backend,
                max_side=args.ssim_max_side,
            )
        else:
            print("No frames were extracted. Cannot proceed.")
            return
    elif args.detection_backend == "hybrid-keyframe" and not use_dynamic:
        slide_changes = hybrid_keyframe_slide_changes(
            args.video_file,
            args.output_dir,
            args.interval,
            args.threshold,
            layout_info,
            max_duration_sec=args.max_duration,
            detect_width=args.detect_width,
            image_format=args.image_format,
            refine_workers=args.materialize_workers,
        )
        if slide_changes is None:
            print("Hybrid keyframe mode unavailable; falling back to accurate full scan.")

        if slide_changes is not None:
            slide_changes = materialize_slide_frames(
                args.video_file,
                slide_changes,
                layout_info=layout_info,
                dynamic_layout=False,
                image_format=args.image_format,
                jpeg_quality=args.jpeg_quality,
                workers=args.materialize_workers,
            )
        else:
            slide_changes = extract_slide_changes_streaming(
                args.video_file,
                args.output_dir,
                args.interval,
                args.threshold,
                layout_info=layout_info,
                dynamic_layout=use_dynamic,
                max_duration_sec=args.max_duration,
                ssim_max_side=args.ssim_max_side,
                output_max_side=args.output_max_side,
            )
    else:
        slide_changes = extract_slide_changes_streaming(
            args.video_file,
            args.output_dir,
            args.interval,
            args.threshold,
            layout_info=layout_info,
            dynamic_layout=use_dynamic,
            max_duration_sec=args.max_duration,
            ssim_max_side=args.ssim_max_side,
            output_max_side=args.output_max_side,
        )

    if slide_changes:
        generate_markdown(slide_changes, args.output_dir, args.md_name)
    else:
        print("No slide-change frames were extracted. Cannot proceed.")


if __name__ == "__main__":
    main()
