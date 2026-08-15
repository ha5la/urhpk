"""Progress bars for the render's long stages.

A render is hours long and each stage runs at its own rate, so the bars are
per-stage: one undifferentiated bar over the lot would say nothing useful about
when the thing finishes, which is the question being asked.

One knob rather than two rendering paths. An unattended render is redirected to
a log, where an in-place bar redrawn ten times a second writes a megabyte of
carriage returns; off a terminal it redraws once every half minute instead, so
a stage leaves a handful of updates. `tail -f` still shows them live, and the
finished log holds each stage's last state.
"""

import sys
from typing import TextIO

from tqdm import tqdm

LOG_INTERVAL_S = 30.0


def stage_bar(desc: str, total: int, unit: str = "frame", stream: TextIO | None = None):
    out = sys.stderr if stream is None else stream
    return tqdm(
        desc=desc,
        total=total,
        unit=unit,
        file=out,
        mininterval=0.1 if out.isatty() else LOG_INTERVAL_S,
    )
