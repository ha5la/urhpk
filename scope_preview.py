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
    uv run scope_preview.py RECORDING.scope [-o out.mp4] [--scale N] [--rows N]
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np

from icom_net import read_scope_records

SCOPE_AMP_MAX = 160

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


def render(scope_path: Path, out_path: Path, scale: int, rows: int) -> None:
    sweeps = read_sweeps(scope_path)
    if len(sweeps) < 2:
        raise SystemExit(f"need at least 2 sweeps in {scope_path}, found {len(sweeps)}")

    npix = len(sweeps[0][3])
    width = npix * scale
    # Real sweep arrival isn't evenly spaced -- fps is just the recording's
    # own average rate, clamped to a sane playback range. Fine for a preview;
    # frame-accurate timing only matters once this feeds a synced render.
    dt = (sweeps[-1][0] - sweeps[0][0]) / (len(sweeps) - 1)
    fps = max(1.0, min(30.0, 1.0 / dt)) if dt > 0 else 10.0

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
        for _, _, _, pixels in sweeps:
            row = np.repeat(lut[pixels], scale, axis=0)  # (npix, 3) -> (width, 3)
            canvas[1:] = canvas[:-1]  # scroll down; newest sweep enters at the top
            canvas[0] = row
            proc.stdin.write(canvas.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()

    start_hz0, end_hz0 = sweeps[0][1], sweeps[0][2]
    print(
        f"wrote {out_path}: {len(sweeps)} sweeps, {fps:.2f} fps, "
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
    args = ap.parse_args()
    render(args.scope_file, args.out, args.scale, args.rows)


if __name__ == "__main__":
    main()
