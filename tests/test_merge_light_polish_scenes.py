import runpy
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "06_merge_light_polish_scenes.py"
MODULE = runpy.run_path(str(SCRIPT))


def test_build_markdown_keeps_one_ppt_per_section_and_removes_cue_timestamps():
    source = """Intro

## Slide 1 (Timestamp: 00:00:00.000)

![Slide 1](ppt_pics/one.png)

**`[0:00:00 - 0:00:01]`**
第一句，

**`[0:00:01 - 0:00:02]`**
接着说完。

---

## Slide 2 (Timestamp: 00:00:02.000)

![Slide 2](ppt_pics/two.png)

**`[0:00:02 - 0:00:03]`**
第二页单独成段。

---
"""
    preamble, slides = MODULE["parse_slides"](source)
    result = MODULE["build_markdown"](preamble, slides, 240)

    assert result.count("## PPT ") == 2
    assert result.count("![PPT ") == 2
    assert "## PPT 1" in result and "第一句，接着说完。" in result
    assert "## PPT 2" in result and "第二页单独成段。" in result
    assert "**`[" not in result
