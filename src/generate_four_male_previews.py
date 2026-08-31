"""Generate four complete first-chapter WAV previews, one folder per voice.

Run from the repository root:

    C:\\DevTools\\Python312\\python.exe src\\generate_four_male_previews.py

Interrupted runs are resumable because temporary segments remain until that
voice has been merged successfully.  On success, each Chinese-named folder
contains exactly one final WAV file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREVIEW_SCRIPT = Path(__file__).with_name("prosody_preview.py")
OUTPUT_ROOT = ROOT / "Workspace" / "四種男聲_1.25倍"

VOICES = (
    ("雲希", "zh-CN-YunxiNeural"),
    ("雲揚", "zh-CN-YunyangNeural"),
    ("雲健", "zh-CN-YunjianNeural"),
    ("雲夏", "zh-CN-YunxiaNeural"),
)


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    # Create the complete requested folder layout up front, even before the
    # first network TTS request. An interrupted run therefore still has all
    # four clearly named destinations.
    for display_name, _voice_id in VOICES:
        (OUTPUT_ROOT / display_name).mkdir(parents=True, exist_ok=True)

    for display_name, voice_id in VOICES:
        output_dir = OUTPUT_ROOT / display_name
        print(f"\n=== {display_name} ({voice_id}) ===", flush=True)
        subprocess.run(
            [
                sys.executable,
                str(PREVIEW_SCRIPT),
                "--voice",
                voice_id,
                "--rate",
                "+25%",
                "--output-dir",
                str(output_dir),
                "--final-only",
            ],
            cwd=ROOT,
            check=True,
        )

    print(f"\n完成：{OUTPUT_ROOT}", flush=True)


if __name__ == "__main__":
    main()
