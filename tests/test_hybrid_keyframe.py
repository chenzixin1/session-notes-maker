import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR_PATH = ROOT / "scripts" / "02_extract_slide_timestamps.py"
RUNNER_PATH = ROOT / "scripts" / "00_build_session_notes.py"


def load_extractor():
    spec = importlib.util.spec_from_file_location("session_notes_slides", EXTRACTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HybridKeyframeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_extractor()

    def test_cli_defaults_to_hybrid_keyframe_and_png(self):
        extractor_help = subprocess.run(
            [sys.executable, str(EXTRACTOR_PATH), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        runner_help = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        for output in (extractor_help, runner_help):
            self.assertIn("--detection-backend", output)
            self.assertIn("hybrid-keyframe", output)
            self.assertIn("--image-format", output)

    def test_window_merge_combines_overlapping_refine_ranges(self):
        merged = self.module._merge_windows([(12.0, 20.0), (0.0, 4.0), (3.0, 8.0)])
        self.assertEqual(merged, [(0.0, 8.0), (12.0, 20.0)])

    def test_hybrid_snaps_isolated_keyframe_candidate_to_interval(self):
        black = np.zeros((8, 8), dtype=np.uint8)
        white = np.full((8, 8), 255, dtype=np.uint8)
        keyframes = [
            {"time_sec": 0.0, "filename": "frame_0.png", "detect_gray": black},
            {"time_sec": 5.1, "filename": "frame_5.png", "detect_gray": white},
        ]

        with tempfile.TemporaryDirectory() as output_dir:
            with (
                patch.object(self.module, "_video_duration", return_value=20.0),
                patch.object(
                    self.module,
                    "extract_keyframe_detection_frames_ffmpeg",
                    return_value=keyframes,
                ),
                patch.object(
                    self.module,
                    "find_slide_changes",
                    return_value=[keyframes[1]],
                ),
                patch.object(self.module, "extract_detection_window_ffmpeg", return_value=[]),
            ):
                slides = self.module.hybrid_keyframe_slide_changes(
                    "video.mp4",
                    output_dir,
                    interval_sec=2.0,
                    threshold=0.9,
                    layout_info={"ppt_rect": (0, 0, 100, 100)},
                    refine_workers=1,
                )

        self.assertEqual([slide["time_sec"] for slide in slides], [0.0, 6.0])

    def test_hybrid_returns_none_when_keyframe_scan_is_unavailable(self):
        with tempfile.TemporaryDirectory() as output_dir:
            with (
                patch.object(self.module, "_video_duration", return_value=20.0),
                patch.object(
                    self.module,
                    "extract_keyframe_detection_frames_ffmpeg",
                    return_value=None,
                ),
            ):
                slides = self.module.hybrid_keyframe_slide_changes(
                    "video.mp4",
                    output_dir,
                    interval_sec=2.0,
                    threshold=0.9,
                    layout_info={"ppt_rect": (0, 0, 100, 100)},
                )

        self.assertIsNone(slides)


if __name__ == "__main__":
    unittest.main()
