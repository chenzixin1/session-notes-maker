import importlib.util
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "polish_slide_transcript",
    SCRIPT_DIR / "04_polish_slide_transcript.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_slide_number_comes_from_header_when_preamble_shifts_segment_index():
    segment = {"header": "## Slide 1 (Timestamp: 00:00:00.000)"}
    assert module._segment_slide_number(segment, fallback_idx=1) == 1


def test_slide_number_falls_back_to_one_based_segment_index():
    segment = {"header": "## Introduction"}
    assert module._segment_slide_number(segment, fallback_idx=2) == 3
