"""
从演讲视频中抽取关键帧并生成带时间戳的 Markdown 索引。

该脚本属于流程的第 02 步：
1. 按固定时间间隔截取视频画面，保存到 `ppt_pics/`
2. 基于帧间相似度检测幻灯片切换节点
3. 生成 `ppt_timestamps.md`，记录每张幻灯片的时间戳与预览图

Usage:
    python 02_extract_slide_timestamps.py input_video.mp4 [-o OUTPUT_DIR] [-i INTERVAL] [-t THRESHOLD]
        [--md_name MD_NAME] [-I | --interactive] [--rect_file PATH] [--force_reselect]
        [--ppt_rect x1,y1,x2,y2] [--max_duration SEC]

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
from skimage.metrics import structural_similarity as ssim
import numpy as np
import datetime
import math
import concurrent.futures  # Added for threading
from typing import Dict, List, Optional, Tuple

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

def extract_frames(
    video_path,
    output_dir,
    interval_sec,
    layout_info=None,
    dynamic_layout: bool = True,
    layout_refresh_interval: float = 10.0,
    max_duration_sec: float = None,
):
    """Extracts frames from video at specified intervals.

    When layout_info indicates mixed content, frames are cropped to the PPT region before saving.
    """
    print(f"Extracting frames from '{video_path}' every {interval_sec} seconds...")

    # Create main output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created directory: {output_dir}")

    # Create ppt_pics subdirectory for images
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
    
    # Limit duration if max_duration_sec is specified
    if max_duration_sec is not None and max_duration_sec > 0:
        duration_sec = min(duration_sec, max_duration_sec)
        print(f"Limiting extraction to first {max_duration_sec} seconds ({format_time(max_duration_sec)})")

    print(f"Video Info: FPS={fps:.2f}, Total Frames={total_frames}, Duration={format_time(duration_sec)}")

    extracted_frames = []
    current_time_sec = 0.0

    current_layout = layout_info
    last_layout_update_time = -1e9

    while current_time_sec <= duration_sec:
        frame_id = int(current_time_sec * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
        ret, frame = cap.read()

        if not ret:
            # Might happen at the very end or if seeking fails
            print(f"Warning: Could not read frame at {format_time(current_time_sec)}")
            current_time_sec += interval_sec
            continue

        timestamp_str_file = format_time(current_time_sec).replace(":", "_").replace(".", "_")
        img_filename = f"frame_{timestamp_str_file}.png"

        # Save to ppt_pics subdirectory
        pics_dir = os.path.join(output_dir, "ppt_pics")
        img_path = os.path.join(pics_dir, img_filename)

        # 根据需要动态更新 PPT 区域：场景切换时自动调整截图区域
        if dynamic_layout and (current_layout is None or current_time_sec - last_layout_update_time >= layout_refresh_interval):
            current_layout = _detect_ppt_rect_in_single_frame(frame)
            last_layout_update_time = current_time_sec
            print(f"[Dynamic layout] t={format_time(current_time_sec)} PPT rect={current_layout.get('ppt_rect')}")

        frame_to_save = frame
        active_layout = current_layout if dynamic_layout else layout_info

        if active_layout and active_layout.get("ppt_rect"):
            x, y, w, h = active_layout["ppt_rect"]
            if w > 0 and h > 0:
                x2 = min(x + w, frame.shape[1])
                y2 = min(y + h, frame.shape[0])
                x = max(0, min(x, frame.shape[1] - 1))
                y = max(0, min(y, frame.shape[0] - 1))
                if x < x2 and y < y2:
                    frame_to_save = frame[y:y2, x:x2].copy()
                else:
                    print("Warning: PPT crop rectangle invalid. Falling back to full frame.")
            else:
                print("Warning: PPT crop rectangle has non-positive dimensions. Falling back to full frame.")

        cv2.imwrite(img_path, frame_to_save)

        # Store the relative path for markdown (ppt_pics/filename.png)
        relative_path = os.path.join("ppt_pics", img_filename)
        extracted_frames.append({'time_sec': current_time_sec, 'path': img_path, 'filename': img_filename, 'relative_path': relative_path})

        # Print progress sparsely
        if len(extracted_frames) % 20 == 0:
             print(f"  Extracted frame at {format_time(current_time_sec)}")


        current_time_sec += interval_sec
        # Small safety break for potential infinite loops with faulty videos
        if interval_sec <= 0:
             print("Error: Interval must be positive.")
             break
        if len(extracted_frames) > (duration_sec / interval_sec) * 2 and duration_sec > 0: # Heuristic check
            print("Warning: Extracted significantly more frames than expected. Stopping.")
            break


    cap.release()
    print(f"Finished extracting {len(extracted_frames)} frames.")
    return extracted_frames

def compare_frame_pair(prev_frame_info, current_frame_info, threshold, index):
    """Helper function to compare a single pair of frames for SSIM."""
    try:
        # Use the full path to read the images
        prev_img = cv2.imread(prev_frame_info['path'])
        curr_img = cv2.imread(current_frame_info['path'])

        if prev_img is None or curr_img is None:
            print(f"Warning: Could not read image for comparison: {prev_frame_info['path'] if prev_img is None else ''} {current_frame_info['path'] if curr_img is None else ''}")
            return None # Cannot compare if images are missing

        prev_img_gray = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY)
        curr_img_gray = cv2.cvtColor(curr_img, cv2.COLOR_BGR2GRAY)

        if prev_img_gray.shape != curr_img_gray.shape:
            print(f"Warning: Frame dimensions mismatch between {prev_frame_info['path']} and {current_frame_info['path']}. Skipping comparison.")
            # Consider this a change? Or skip? Let's skip for now.
            # If skipped, this frame won't be added unless the *next* comparison detects a change.
            return None

        # Calculate SSIM
        similarity_index = ssim(prev_img_gray, curr_img_gray, data_range=prev_img_gray.max() - prev_img_gray.min())

        # print(f"  Compared {prev_frame_info['filename']} and {current_frame_info['filename']} (Index {index}): SSIM = {similarity_index:.4f}")

        if similarity_index < threshold:
            # print(f"  Change detected at index {index} (SSIM: {similarity_index:.4f})")
            return index # Return the index of the *current* frame where the change was detected
        return None
    except Exception as e:
        print(f"Error comparing frames {prev_frame_info['path']} and {current_frame_info['path']} (Index {index}): {e}")
        return None # Return None on error

def find_slide_changes(frame_list, threshold=0.9):
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


    # Sort the indices and build the final list of slide changes
    sorted_indices = sorted(list(slide_changes_indices))
    slide_changes = [frame_list[i] for i in sorted_indices]

    # Reporting changes detected
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
    parser.add_argument("--md_name", help="Name for the output markdown file (default: same as output directory name with .md extension).")
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
    if args.ppt_rect:
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
    if manual_rect is None and not args.force_reselect:
        loaded = load_rect_file(rect_file)
        if loaded is not None:
            manual_rect = loaded
            print(f"Loaded PPT rect from sidecar file: {rect_file} -> {manual_rect}")

    # Interactive mode: let the user draw the rect with the mouse
    if args.interactive:
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

    layout_info = detect_mixed_layout(args.video_file, manual_rect=manual_rect)
    if layout_info.get("ppt_rect"):
        print(f"Using PPT crop rect: {layout_info.get('ppt_rect')} (is_mixed={layout_info.get('is_mixed')})")

    # 如果用户指定了手动矩形，就不再做动态检测，以避免尺寸变化导致的抖动
    use_dynamic = manual_rect is None

    # 1. Extract frames
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
        slide_changes = find_slide_changes(extracted_frames, args.threshold)
        # 3. Generate Markdown
        generate_markdown(slide_changes, args.output_dir, args.md_name)
    else:
        print("No frames were extracted. Cannot proceed.")


if __name__ == "__main__":
    main()