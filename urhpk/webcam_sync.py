"""Lining the webcam up with the radio audio.

Where the webcam *starts* depends on how it was recorded: an Alt+V capture is
renamed with a µs-precise stamp off this machine's own clock and needs nothing
further, while an independently recorded clip has only its own coarse filename
convention. Three sources are tried in descending order of precision -- that
stamp, the logged webcam_start event, and the phone convention.

The *rate* is a separate problem, and reaches the exact starts too: the capture
is timestamped by the laptop's clock and compared against the radio's sample
clock, which is a different crystal. So the start is refined by cross-
correlating the webcam's audio against the radio's, segment by segment, and the
refinement fits a line rather than a constant -- an offset that is right at the
start is visibly wrong two hours later.

How much rate is really left is unmeasured on a well-disciplined clock: the
+2.487 s/hour fitted on the August round was measured with systemd-timesyncd
restarted by hand shortly before it and no chrony, so an unknown part of that
was the laptop still slewing rather than the two crystals disagreeing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import NamedTuple

import numpy as np

from urhpk.timeline import Qso, Segment, audio_time_for, derive_utc_offset
from urhpk.wav import read_wav_range
from urhpk.webcam_face import FaceScan

_WEBCAM_TS_RE = re.compile(r"(\d{8}_\d{6})")


class WebcamClip(NamedTuple):
    """One capture on the output timeline: where its own frame 0 lands
    (seconds into the video) and the clock-drift rate its timeline is scaled
    by. A round has one per Alt+V start/stop pair.

    `face` is filled in later, by webcam_face, and is None when the detector
    was unavailable -- the clip is placed in time here, framed there."""

    path: str
    start: float
    rate: float = 0.0
    face: FaceScan | None = None


def parse_webcam_wall(path: str) -> datetime:
    """Parse a phone/webcam filename's embedded timestamp (e.g.
    VID_20260706_180003.mp4) the same way scan_segments reads WAV filenames."""
    m = _WEBCAM_TS_RE.search(os.path.basename(path))
    if not m:
        raise ValueError(f"no YYYYMMDD_HHMMSS timestamp found in {path}")
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")


_WEBCAM_PRECISE_RE = re.compile(r"-webcam-(\d{8}T\d{6}\.\d+Z)\.")


def parse_webcam_precise_filename(path: str) -> datetime | None:
    """Parse the exact, µs-precise UTC timestamp recorders.webcam_finalize_name
    bakes into the filename a second into the capture (e.g.
    `foo-webcam-20260722T121101.868307Z.mp4`), read from the ffmpeg capture
    log's own frame-0 wallclock. Preferred over webcam_start_wall below: exact
    where that event is ~1s early, self-contained in the filename itself -- no
    dependency on the sidecar `.log` file surviving alongside the video -- and
    a rename is free (no second copy of the video data, unlike tagging the
    file's own container metadata after the fact -- see CLAUDE.md). Returns
    None for a recording made before this existed, or the coarse phone-clip
    filename convention (parse_webcam_wall) instead."""
    m = _WEBCAM_PRECISE_RE.search(os.path.basename(path))
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S.%fZ")


def sync_webcam_start(
    cam_wall: datetime,
    cam_dur: float,
    qsos: list[Qso],
    segs: list[Segment],
    offset_h: int,
) -> float:
    """Video-timeline position (seconds) where the webcam recording begins.

    The webcam is a separate device with its own clock convention, which
    need not match the WAV recorder's (in practice the WAV recorder here
    stamped filenames in plain UTC, while the phone stamped its own in local
    wall time -- two different offsets for the same round). So its offset
    can't be assumed to equal `offset_h`; it's derived the same way
    `offset_h` itself was, by treating the whole webcam clip as a one-segment
    "recording" and reusing derive_utc_offset's span-midpoint match against
    the *full* QSO list (not any --duration-trimmed subset, since a short
    preview's QSO span is too narrow an anchor for reliable hour rounding).
    """
    cam_seg = Segment("", cam_wall, cam_dur, 0.0)
    cam_offset_h = derive_utc_offset([cam_seg], qsos)
    cam_utc_start = cam_wall - timedelta(hours=cam_offset_h)
    return audio_time_for(cam_utc_start + timedelta(hours=offset_h), segs)


def _read_webcam_audio_range(
    path: str, t0: float, dur: float, sr: int = 16000
) -> tuple[np.ndarray, int]:
    """Read `dur` seconds of audio starting at `t0` from a video file's own
    audio track, resampled to mono `sr` -- for cross-correlating against the
    radio's own WAV audio (see refine_webcam_start). Returns an empty array
    if `t0` is negative or the file has no audio track."""
    if t0 < 0 or dur <= 0:
        return np.array([]), sr
    try:
        out = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{t0:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sr),
                "-f",
                "s16le",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError:
        return np.array([]), sr
    return np.frombuffer(out, dtype=np.int16).astype(float), sr


def _rms_envelope(x: np.ndarray, sr: int, win_s: float = 0.05) -> np.ndarray:
    """RMS amplitude in consecutive win_s windows -- a coarse speech-rhythm
    signature, robust to the very different frequency/timbre characteristics
    of two different microphones/paths recording the same speech (see
    refine_webcam_start), unlike correlating raw waveform samples directly."""
    win = max(1, int(sr * win_s))
    n = len(x) // win
    if n == 0:
        return np.array([])
    return np.sqrt(np.mean(x[: n * win].reshape(n, win) ** 2, axis=1))


def _find_offset_correction(
    radio_audio: np.ndarray,
    radio_sr: int,
    webcam_audio: np.ndarray,
    webcam_sr: int,
    padding_s: float,
    env_win_s: float = 0.05,
) -> tuple[float, float]:
    """Cross-correlate the envelope of `radio_audio` (one known TX segment)
    against a `webcam_audio` window that starts `padding_s` earlier than the
    coarse webcam_start would predict, spanning padding_s extra on each end.

    Returns (correction, confidence): `correction` is the number of seconds
    to add to the coarse webcam_start so this segment's audio aligns with
    its match in webcam_audio (0.0 if nothing usable). `confidence` is the
    peak's normalized correlation (roughly 0..1; higher is more trustworthy,
    see refine_webcam_start's min_confidence)."""
    r_env = _rms_envelope(radio_audio, radio_sr, env_win_s)
    w_env = _rms_envelope(webcam_audio, webcam_sr, env_win_s)
    if len(r_env) < 3 or len(w_env) < len(r_env):
        return 0.0, 0.0
    r_env = r_env - r_env.mean()
    w_env = w_env - w_env.mean()
    r_norm = float(np.linalg.norm(r_env))
    if r_norm == 0:
        return 0.0, 0.0
    corr = np.correlate(w_env, r_env, mode="valid")
    best_idx = int(np.argmax(corr))
    w_local_norm = float(np.linalg.norm(w_env[best_idx : best_idx + len(r_env)]))
    confidence = corr[best_idx] / (r_norm * w_local_norm) if w_local_norm > 0 else 0.0
    correction = padding_s - best_idx * env_win_s
    return correction, float(confidence)


def refine_webcam_start(
    webcam_path: str,
    segs: list[Segment],
    webcam_start: float,
    max_anchors: int = 20,
    padding_s: float = 8.0,
    min_confidence: float = 0.3,
) -> tuple[float, float, int]:
    """Refine the coarse (whole-hour) webcam_start via audio cross-
    correlation against the operator's own TX audio, fitting a *linear
    drift* model rather than a single constant correction.

    sync_webcam_start/derive_utc_offset only correct whole-hour clock
    *offset* differences (timezone/DST) between the two *independent*
    recording devices (phone, radio recorder) -- by design, since that's
    all whole-hour rounding can express. But two independent consumer
    clocks (phone system clock, IC-9700 recorder clock) also don't tick at
    exactly the same *rate* -- a small, real crystal-oscillator mismatch
    that produces a correction growing roughly linearly with elapsed time,
    not a constant. Found from a real reported case, confirmed by ear (the
    operator's own voice reaches the phone's own mic and the radio's mic at
    the same real-world instant, but drifted apart across the *output*
    timeline the further into the round): sampling confident anchors
    across a real ~2-hour round showed the correction growing smoothly
    from ~0s near the start to ~+3.2s near the end -- not a frame-rate or
    rendering bug (that was checked and fixed separately; see
    decode_cw_subranges's neighbour docstrings), and not something a single
    constant offset (the first version of this function) can correct.

    Anchors are `s.ptt` (TX) segments at least 1.5s long, sampled evenly
    across the *whole* candidate list (not just the first `max_anchors`,
    which -- found from the same real case -- clustered in the first few
    minutes and produced a rate estimate with almost no time range to
    constrain it). The phone's mic only picks up the operator's *own*
    voice, not what's coming through a headset from the other station, so
    only TX (not RX) segments have anything in the webcam audio to match
    against. Each anchor's own radio audio (read straight from its WAV
    file) is cross-correlated against a padded window of webcam audio via
    _find_offset_correction; anchors below min_confidence are dropped as
    unreliable (real data: false correlation peaks on noisy/short segments
    scored confidence 0.08-0.29, genuine matches scored 0.34-0.77 -- a
    clean gap at 0.3).

    A per-anchor `(audio_t, correction)` pair is then fit with a degree-1
    least-squares line (np.polyfit): the intercept becomes the corrected
    webcam_start (matching the original constant-offset meaning at
    audio_t=0), and the slope is returned as a *rate* -- see render()'s
    setpts usage for how this rate is applied as a timeline stretch, since
    a linear drift can't be corrected by -itsoffset (a constant shift)
    alone.

    Returns (corrected_webcam_start, rate, n_confident_anchors). With fewer
    than 2 confident anchors, rate is 0.0 (not enough points to fit a
    line) and webcam_start is nudged by the single anchor's own correction
    if exactly one was confident, or left unchanged if none were."""
    candidates = [s for s in segs if s.ptt and s.dur >= 1.5]
    step = max(1, len(candidates) // max_anchors)
    sample = candidates[::step]
    audio_ts: list[float] = []
    corrections: list[float] = []
    for seg in sample:
        radio_audio, radio_sr = read_wav_range(seg.path, 0.0, seg.dur)
        if len(radio_audio) == 0:
            continue
        src_start = seg.audio_t - webcam_start - padding_s
        cam_audio, cam_sr = _read_webcam_audio_range(
            webcam_path, src_start, seg.dur + 2 * padding_s
        )
        if len(cam_audio) == 0:
            continue
        correction, confidence = _find_offset_correction(
            radio_audio, radio_sr, cam_audio, cam_sr, padding_s
        )
        if confidence >= min_confidence:
            audio_ts.append(seg.audio_t)
            corrections.append(correction)
    if not corrections:
        return webcam_start, 0.0, 0
    n = len(corrections)
    if n == 1:
        return webcam_start + corrections[0], 0.0, 1
    fit_t = np.array(audio_ts)
    fit_c = np.array(corrections)
    # A few spurious correlation peaks (confidence >= min_confidence but a
    # wildly inconsistent correction -- e.g. another voice briefly matching
    # by chance) can skew a single least-squares fit substantially. Found
    # on a real ~2h same-machine webcam recording: one naive fit put the
    # whole-round drift at +3.4s; iteratively rejecting outliers (>1.5
    # std from the running fit) and refitting converged to +5.1s with the
    # residual std dropping from ~2.6s to ~0.1s -- the robust fit is the
    # trustworthy one. Only attempted with >=4 points; fewer than that
    # can't reliably tell an outlier from real curvature.
    if n >= 4:
        for _ in range(4):
            rate, intercept = np.polyfit(fit_t, fit_c, 1)
            resid = fit_c - (rate * fit_t + intercept)
            std = resid.std()
            if std == 0:
                break
            keep = np.abs(resid) < 1.5 * std
            if keep.sum() == len(fit_t) or keep.sum() < 4:
                break
            fit_t, fit_c = fit_t[keep], fit_c[keep]
    rate, intercept = np.polyfit(fit_t, fit_c, 1)
    return webcam_start + float(intercept), float(rate), n


def webcam_start_wall(path: str) -> datetime | None:
    """The UTC wall-clock start of an Alt+V logger-recorded webcam, read from
    a puskas_logger `*-input.jsonl` log's `webcam_start` event (returns the
    first one's `t`, or None if the file has no such event).

    A logger-recorded webcam is captured on the *same machine* as the logger,
    so start/stop go through the same `datetime.now(timezone.utc)` that already
    stamps every QSO and keystroke -- the recording's real start time is known
    exactly, with no separate device clock to reconcile. That makes its sync
    exact, like the asciinema cast (see parse_cast_header) and unlike a phone
    clip (parse_webcam_wall + sync_webcam_start + refine_webcam_start, which
    exist only to recover a *different* device's whole-hour offset and fit its
    clock-drift rate). None here means the input log predates the Alt+V webcam
    feature -- fall back to the filename-timestamp phone path."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "webcam_start":
                try:
                    return datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%S.%fZ")
                except (KeyError, ValueError):
                    return None
    except OSError:
        return None
    return None
