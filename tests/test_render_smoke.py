"""Run the real render on a real round's files and check what it produces.

Opt-in (`-m smoke`), and it skips unless `test/` -- a recorded 169-second round
kept outside git -- is present next to the checkout.

The oracle is the pair of text files, not the video: `chapters.txt` and `.srt`
are exact, and they are written before the costly part starts, so `--no-video`
answers in about a second. A full render of the same round is ~6 minutes and
tells you nothing more about the timeline; what it would tell you is whether
the picture is right, and no assertion here can judge that.

Every side input the round left behind is passed, including the ones that
cannot move a caption: a `.cast` or `.scope` that no longer parses is a real
regression, and this is the only test that reads either from a real recording.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parent.parent
ROUND = ROOT / "test"
CONTEST_VIDEO = str(ROOT / "contest_video.py")

CHAPTERS = """\
0:00 Start
0:29 QSO 001 HA5MIG  2M FM
0:50 QSO 002 HA5MIG  2M SSB
1:27 QSO 003 HA1VHF  2M CW
"""

SRT = """\
1
00:00:29,025 --> 00:00:37,025
QSO 001 HA5MIG  2M FM

2
00:00:49,943 --> 00:00:57,943
QSO 002 HA5MIG  2M SSB

3
00:01:27,093 --> 00:01:35,093
QSO 003 HA1VHF  2M CW
"""


class Render(NamedTuple):
    stem: Path  # the output path with its suffix dropped
    stdout: str


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if not ROUND.is_dir():
        pytest.skip(f"no recorded round at {ROUND}")

    tmp = tmp_path_factory.mktemp("render")
    out = tmp / "out.mp4"
    proc = subprocess.run(
        [
            "uv",
            "run",
            CONTEST_VIDEO,
            "recording",
            "260811-HA5LA-2M.edi",
            "--telemetry",
            "260811-HA5LA-telemetry.jsonl",
            "--input-log",
            "260811-HA5LA-input.jsonl",
            "--cast",
            "2026-08-11T19:16:06+00:00.cast",
            "--scope",
            "260811-HA5LA.scope",
            "--no-video",
            "-o",
            str(out),
        ],
        cwd=ROUND,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return Render(tmp / "out", proc.stdout)


def test_the_chapters_are_the_ones_the_round_earned(rendered):
    assert rendered.stem.with_suffix(".chapters.txt").read_text() == CHAPTERS


def test_the_captions_are_the_ones_the_round_earned(rendered):
    assert rendered.stem.with_suffix(".srt").read_text() == SRT


def test_no_video_stops_before_the_render(rendered):
    """The flag's whole point: none of the expensive outputs exist."""
    assert not rendered.stem.with_suffix(".mp4").exists()
    assert not rendered.stem.with_suffix(".concat.wav").exists()


if __name__ == "__main__":  # a quick manual run without pytest
    sys.exit(pytest.main([__file__, "-m", "smoke", "-q", "--no-cov"]))
