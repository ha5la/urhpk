"""The ffmpeg capture log written next to a logger-recorded webcam.

`*-webcam.log` is the logger's own stderr capture of the ffmpeg process, and
the only place the webcam's true frame-0 instant is ever written down. The
logger reads it back a second into the capture to rename the recording with
that timestamp; from there the filename carries it, and the video reads the
filename to place the PiP on the timeline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

_INPUT_RE = re.compile(r"^Input #\d+, ([^,]+)")
_START_RE = re.compile(r"start:\s*([0-9]+\.[0-9]+)")


def frame_zero_utc(log_path: str) -> datetime | None:
    """Precise UTC wallclock of the webcam's first frame, or None.

    ffmpeg prints each input's own capture start time under its `Input #N`
    header. With -use_wallclock_as_timestamps 1 (see
    recorders._webcam_capture_cmd) the v4l2 video input's `start:` is a true
    Unix epoch -- the exact real-world instant of the first frame, to the
    microsecond. Without the flag it's CLOCK_MONOTONIC (uptime), useless as an
    absolute time; the PulseAudio input always reports a wallclock epoch, so we
    prefer the video input's `start:` (it's what the PiP shows) and fall back
    to audio, distinguishing the two by magnitude (a Unix epoch is > 1e9; an
    uptime is far smaller). Returns None if neither is an absolute epoch (an
    old recording without the flag and, implausibly, no usable audio start) or
    the log can't be read.
    """
    video_epoch = audio_epoch = None
    cur = None
    try:
        for line in open(log_path, encoding="utf-8", errors="replace"):
            m = _INPUT_RE.match(line.strip())
            if m:
                cur = m.group(1)
                continue
            if cur and "start:" in line:
                sm = _START_RE.search(line)
                if sm and float(sm.group(1)) > 1e9:  # a Unix epoch, not uptime
                    val = float(sm.group(1))
                    if ("v4l2" in cur or "video4linux" in cur) and video_epoch is None:
                        video_epoch = val
                    elif "pulse" in cur and audio_epoch is None:
                        audio_epoch = val
                cur = None
    except OSError:
        return None
    epoch = video_epoch if video_epoch is not None else audio_epoch
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
