"""The spectrum-scope waterfall background.

Real IC-9700 CI-V scope sweeps recorded by icom_net.py into a `.scope` file,
rendered straight into video — not showspectrum's reconstruction from the
recorded audio. See icom_net.py for where the sweeps come from; this module
only draws them.
"""

from __future__ import annotations

import subprocess

import numpy as np

from urhpk.icom_net import read_scope_records
from urhpk.video_format import RENDER_FPS

# ---------------------------------------------------------------------------

SCOPE_AMP_MAX = 160  # Icom's own linear scope units, not dBm (see write_scope_record)

SCOPE_WATERFALL_SPAN_S = 10.0  # seconds of history the canvas height represents,
# matching the real IC-9700 display: a signal takes ~4-5s to
# scroll through half the physical waterfall's height there.

SCOPE_STALL_S = 1.0  # no sweep for this long = the stream stopped, not a slow one:
# they arrive at ~29/s, so a second of silence is ~29 missing.

# Classic SDR waterfall gradient: black -> blue -> cyan -> green -> yellow -> red.
_SCOPE_COLORMAP_STOPS = [
    (0, (0, 0, 0)),
    (32, (0, 0, 180)),
    (64, (0, 180, 220)),
    (96, (0, 200, 0)),
    (128, (230, 210, 0)),
    (160, (255, 0, 0)),
]


def _scope_colormap() -> np.ndarray:
    lut = np.zeros((SCOPE_AMP_MAX + 1, 3), dtype=np.uint8)
    for (x0, c0), (x1, c1) in zip(_SCOPE_COLORMAP_STOPS, _SCOPE_COLORMAP_STOPS[1:]):
        for i in range(x0, x1 + 1):
            t = (i - x0) / (x1 - x0)
            lut[i] = [round(c0[ch] + t * (c1[ch] - c0[ch])) for ch in range(3)]
    return lut


def _resize_scope_row(pixels: np.ndarray, width: int) -> np.ndarray:
    """Linearly interpolate a sweep's raw amplitude values (not yet
    colormapped) up to the output canvas width -- interpolating on the
    scalar amplitude domain rather than on already-colormapped RGB triples
    avoids odd color mixing at the boundary between two very different
    colormap regions (e.g. black next to red)."""
    src_x = np.linspace(0, len(pixels) - 1, len(pixels))
    dst_x = np.linspace(0, len(pixels) - 1, width)
    return np.interp(dst_x, src_x, pixels).astype(np.uint8)


def render_scope_video(
    scope_path: str,
    out_path: str,
    W: int,
    H: int,
    fps: int = RENDER_FPS,
    span_s: float = SCOPE_WATERFALL_SPAN_S,
    max_duration: float | None = None,
) -> None:
    """Render a .scope recording into a standalone full-canvas waterfall
    clip, whose t=0 is exactly the first sweep's own timestamp -- that's
    what lets render() position it with a plain -itsoffset the same way
    render_cast_video's output is positioned: both carry real, absolute
    timestamps from the moment they were captured, unlike the webcam
    branch's independent, drifting camera clock.

    Rows scroll on a fixed real-time clock (one new row every span_s/H
    seconds), *not* one row per real sweep -- an earlier version did the
    latter, which meant the canvas height represented however many seconds
    happened to fit at whatever the recording's actual sweep rate was
    (288s for a 720-row canvas at one real synthetic-test sweep every
    0.4s), not a fixed, chosen span. Reported by directly comparing a
    rendered preview against the radio's own physical display: a signal
    there takes ~4-5s to cross half the waterfall's height, i.e. the full
    height is a ~10s window -- span_s defaults to that. Each row shows
    whichever sweep was most recent as of that row's own point on the
    fixed time grid: this naturally compresses periods where sweeps
    arrived faster than the row rate (only the latest-before-the-tick one
    is shown, the rest are skipped) and holds the display steady
    (duplicating the last row) through any stretch slower than the row
    rate -- the same way a real waterfall display behaves when its input
    momentarily stalls. Past SCOPE_STALL_S with no sweep at all the rows
    go black instead: the stream stopped (the radio's own menu closes the
    scope, so this happens whenever the operator goes into it), and a
    black gap says so where a smear of the last sweep would claim signal
    that was never received.
    """
    records = read_scope_records(scope_path)
    if len(records) < 2:
        raise ValueError(
            f"need at least 2 scope sweeps in {scope_path}, found {len(records)}"
        )

    lut = _scope_colormap()
    canvas = np.zeros((H, W, 3), dtype=np.uint8)
    t0 = records[0][0]
    duration = records[-1][0] - t0
    if max_duration is not None:  # same reasoning as render_cast_video's
        duration = min(duration, max_duration)
    row_dt = span_s / H

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
        f"{W}x{H}",
        "-r",
        f"{fps}",
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
        out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        frame_dt = 1.0 / fps
        idx = 0
        n = len(records)
        next_row_t = 0.0
        t = 0.0
        last_idx = -1
        row = None
        while t <= duration:
            while next_row_t <= t:
                while idx + 1 < n and records[idx + 1][0] - t0 <= next_row_t:
                    idx += 1
                if records[idx][0] - t0 <= next_row_t:
                    if idx != last_idx:
                        pixels = np.frombuffer(records[idx][3], dtype=np.uint8)
                        row = lut[_resize_scope_row(pixels, W)]
                        last_idx = idx
                    canvas[1:] = canvas[
                        :-1
                    ]  # scroll down; newest row enters at the top
                    stalled = next_row_t - (records[idx][0] - t0) > SCOPE_STALL_S
                    canvas[0] = 0 if stalled else row
                next_row_t += row_dt
            proc.stdin.write(canvas.tobytes())
            t += frame_dt
    finally:
        proc.stdin.close()
        proc.wait()
