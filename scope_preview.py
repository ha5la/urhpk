#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy"]
# ///
"""
Scope Waterfall Preview
=========================
Renders a .scope recording (written by `icom_net.py --scope`, one binary
record per sweep: timestamp + frequency range + raw amplitude pixels) into
a standalone waterfall video -- a preview of what a scope-driven background
would look like in contest_video.py, before wiring it in for real. Not
synced to any audio/QSO timeline; just the waterfall on its own.

Usage:
    uv run scope_preview.py RECORDING.scope [-o out.mp4] [--scale N] [--rows N] [--span S]
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np

from icom_net import read_scope_records

SCOPE_AMP_MAX = 160
SCOPE_WATERFALL_SPAN_S = 10.0  # seconds of history the canvas height represents --
# matches the real IC-9700 display (a signal takes ~4-5s to
# cross half its physical waterfall's height). Keep this in
# sync with contest_video.py's own SCOPE_WATERFALL_SPAN_S --
# duplicated rather than imported (see render()'s docstring
# for why), so it won't update itself automatically.

# Classic SDR waterfall gradient: black -> blue -> cyan -> green -> yellow -> red.
_COLORMAP_STOPS = [
    (0, (0, 0, 0)),
    (32, (0, 0, 180)),
    (64, (0, 180, 220)),
    (96, (0, 200, 0)),
    (128, (230, 210, 0)),
    (160, (255, 0, 0)),
]


def _build_colormap() -> np.ndarray:
    lut = np.zeros((SCOPE_AMP_MAX + 1, 3), dtype=np.uint8)
    for (x0, c0), (x1, c1) in zip(_COLORMAP_STOPS, _COLORMAP_STOPS[1:]):
        for i in range(x0, x1 + 1):
            t = (i - x0) / (x1 - x0)
            lut[i] = [round(c0[ch] + t * (c1[ch] - c0[ch])) for ch in range(3)]
    return lut


def read_sweeps(path: Path) -> list[tuple[float, int, int, np.ndarray]]:
    return [
        (ts, start_hz, end_hz, np.frombuffer(pixels, dtype=np.uint8))
        for ts, start_hz, end_hz, pixels in read_scope_records(path)
    ]


def render(
    scope_path: Path,
    out_path: Path,
    scale: int,
    rows: int,
    span_s: float = SCOPE_WATERFALL_SPAN_S,
    fps: float = 30.0,
) -> None:
    """Rows scroll on a fixed real-time clock (one new row every span_s/rows
    seconds), not one row per real sweep -- an earlier version did the
    latter, which made the canvas height represent however many seconds
    happened to fit at whatever the recording's actual sweep rate was,
    rather than a fixed, chosen span matching the radio's own display. Each
    row shows whichever sweep was most recent as of that row's point on the
    fixed time grid -- compressing periods where sweeps arrived faster than
    the row rate, holding the display steady through any stretch slower
    than it (or where sweeps stop arriving entirely). See
    contest_video.py's render_scope_video, which this mirrors."""
    sweeps = read_sweeps(scope_path)
    if len(sweeps) < 2:
        raise SystemExit(f"need at least 2 sweeps in {scope_path}, found {len(sweeps)}")

    npix = len(sweeps[0][3])
    width = npix * scale
    t0 = sweeps[0][0]
    duration = sweeps[-1][0] - t0
    row_dt = span_s / rows

    lut = _build_colormap()
    canvas = np.zeros((rows, width, 3), dtype=np.uint8)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{rows}",
        "-r",
        f"{fps:.3f}",
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        frame_dt = 1.0 / fps
        idx = 0
        n = len(sweeps)
        next_row_t = 0.0
        t = 0.0
        last_idx = -1
        row = None
        while t <= duration:
            while next_row_t <= t:
                while idx + 1 < n and sweeps[idx + 1][0] - t0 <= next_row_t:
                    idx += 1
                if sweeps[idx][0] - t0 <= next_row_t:
                    if idx != last_idx:
                        row = np.repeat(lut[sweeps[idx][3]], scale, axis=0)
                        last_idx = idx
                    canvas[1:] = canvas[
                        :-1
                    ]  # scroll down; newest row enters at the top
                    canvas[0] = row
                next_row_t += row_dt
            proc.stdin.write(canvas.tobytes())
            t += frame_dt
    finally:
        proc.stdin.close()
        proc.wait()

    start_hz0, end_hz0 = sweeps[0][1], sweeps[0][2]
    print(
        f"wrote {out_path}: {len(sweeps)} sweeps over {duration:.1f}s, "
        f"{span_s:.0f}s/{rows}rows waterfall, "
        f"{start_hz0 / 1e6:.3f}-{end_hz0 / 1e6:.3f} MHz, {width}x{rows}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("scope_file", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("scope_preview.mp4"))
    ap.add_argument(
        "--scale",
        type=int,
        default=2,
        help="horizontal pixel-duplication factor (default: 2)",
    )
    ap.add_argument(
        "--rows",
        type=int,
        default=400,
        help="waterfall history depth in rows (default: 400)",
    )
    ap.add_argument(
        "--span",
        type=float,
        default=SCOPE_WATERFALL_SPAN_S,
        help=f"seconds of history the full canvas height represents "
        f"(default: {SCOPE_WATERFALL_SPAN_S:.0f}, matching the real IC-9700 display)",
    )
    args = ap.parse_args()
    render(args.scope_file, args.out, args.scale, args.rows, args.span)


if __name__ == "__main__":
    main()
