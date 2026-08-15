"""Where to crop the webcam PiP, given where the operator's face actually is.

The PiP used to take a centred crop, which assumes the operator sits in the
middle of a frame they cannot see: the Alt+V capture has no preview, and a real
2h round put the face at 0.61 of the width, off-centre for a third of the round
and partly outside the crop at worst. FINDINGS.md has the measurements.

One crop per clip, from the median of the face centres, and only the crop's *x*
moves: the head already fills ~64% of the frame height, so there is nothing for
a zoom to gain, and the face's motion within a round is a 0.14% tail that a
tracker would chase at the cost of visible jitter.

The detector is optional -- `uv run --extra render` -- and everything here
degrades to the old centred crop without it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from statistics import median
from typing import NamedTuple

MODEL = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")
DETECT_W, DETECT_H = 640, 360
SAMPLE_S = 5.0
MIN_SCORE = 0.6

# (frame index, x, y, w, h, score), in source pixels
Detection = tuple[int, float, float, float, float, float]


class FaceScan(NamedTuple):
    """What one clip's scan found: the face centre the crop is built from
    (None when nothing was detected at all), the clip's own frame size, and
    how many of the sampled frames had a face in them."""

    cx: float | None
    source: tuple[int, int]
    samples: int
    hits: int


def face_centre(dets: list[Detection], min_score: float = MIN_SCORE) -> float | None:
    """The median x of the faces, one per sampled frame.

    The median rather than a mean because a round contains excursions -- the
    August round had a single 10s one -- and the whole point is that the
    framing does not follow them. Largest face per frame, so someone walking
    through the background does not vote."""
    best: dict[int, Detection] = {}
    for d in dets:
        if d[5] < min_score:
            continue
        if d[0] not in best or d[3] * d[4] > best[d[0]][3] * best[d[0]][4]:
            best[d[0]] = d
    if not best:
        return None
    return median(d[1] + d[3] / 2 for d in best.values())


def face_crop(
    src_w: int,
    src_h: int,
    recess_w: int,
    recess_h: int,
    face_cx: float | None = None,
) -> tuple[int, int, int, int]:
    """The largest crop of the recess's aspect that fits the source, placed on
    the face horizontally. Without a face it is centred, which is what the PiP
    did before this existed.

    Clamped to the frame rather than shrunk: a crop that changes size because
    the operator leaned is the jitter this is avoiding."""
    w = round(min(src_w, src_h * recess_w / recess_h))
    h = round(min(src_h, src_w * recess_h / recess_w))
    if face_cx is None:
        x = (src_w - w) // 2
    else:
        x = min(max(round(face_cx - w / 2), 0), src_w - w)
    return x, (src_h - h) // 2, w, h


def _probe(path: str) -> tuple[int, int, float]:
    out = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    s = out["streams"][0]
    return s["width"], s["height"], float(out["format"]["duration"])


def scan_faces(path: str, sample_s: float = SAMPLE_S, progress=None) -> FaceScan | None:
    """Sample the clip and detect a face in each sampled frame.

    None means the detector is unavailable, which is a different thing from a
    scan that found no face: the first keeps the old behaviour silently
    correct, the second is worth reporting. Costs ~7 minutes on a 2h clip
    against a ~3h render, so it is not cached.

    Detection runs on a 640x360 copy -- YuNet's own cost is per pixel, and the
    face is ~46% of the frame height, far more than it needs."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    src_w, src_h, dur = _probe(path)
    det = cv2.FaceDetectorYN.create(
        str(MODEL), "", (DETECT_W, DETECT_H), MIN_SCORE, 0.3, 5000
    )
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"fps=1/{sample_s},scale={DETECT_W}:{DETECT_H}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    scale = src_w / DETECT_W
    dets: list[Detection] = []
    n = 0
    frame_bytes = DETECT_W * DETECT_H * 3
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        _, faces = det.detect(
            np.frombuffer(buf, np.uint8).reshape(DETECT_H, DETECT_W, 3)
        )
        for f in faces if faces is not None else []:
            dets.append(
                (n, f[0] * scale, f[1] * scale, f[2] * scale, f[3] * scale, f[14])
            )
        n += 1
        if progress:
            progress.update(1)
    proc.stdout.close()
    proc.wait()
    return FaceScan(face_centre(dets), (src_w, src_h), n, len({d[0] for d in dets}))
