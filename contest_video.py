#!/usr/bin/env -S uv run
"""Produce an annotated CW contest video from a recording + EDI log.

Given a directory of timestamped WAV segments (split on RX/TX switches, as
recorded during the contest) and the EDI log for the same round, this builds a
YouTube-ready MP4 with:

  * a scrolling audio spectrogram (SDR-style waterfall) as background
  * a live CW decode ticker, synced to the audio
  * an RX/TX badge, from the WAV files' own rig metadata (the QRG/mode/rotator
    line it used to carry is redundant with the terminal PiP's own toolbar)
  * optionally, a large picture-in-picture of the logger/irssi terminal
    (--cast, an asciinema recording) and a small webcam PiP

The ticker and badge are burned in via one ASS subtitle file in a single
ffmpeg pass; the cast PiP is rendered separately (see
render_cast_video) and composited alongside the webcam PiP in that same pass
-- no frame-by-frame rendering of the main video.

Usage:
    uv run contest_video.py RECORDING_DIR EDI_FILE [-o OUT.mp4]

The WAV filenames must start with a `YYYYMMDD_HHMMSS` local-time stamp (the
format the recorder writes). Segments are concatenated in filename order; the
audio timeline is the sum of segment durations, and wall-clock time (from the
filenames) is used only to line QSOs up against the audio. The EDI QSO times
are UTC; the UTC->local offset is derived automatically from the data, so DST
is handled without configuration.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import statistics
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import edi
import wiring
from cast_render import parse_cast_header, render_cast_video
from cw_decode import (
    MAX_OVER_S,
    CharEvent,
    decode_long_segment,
    decode_segment,
    gate_events,
)
from geo import initial_bearing, maidenhead_to_latlon
from icom_net import band_from_hz, read_scope_records
from wav import parse_wav_title, read_wav_range, read_wav_title

# ---------------------------------------------------------------------------
# Timeline + EDI
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    path: str
    wall: datetime  # local wall-clock start (from filename)
    dur: float  # seconds (full recorded duration)
    audio_t: float  # start offset in the output video (seconds)
    events: list[CharEvent] = field(default_factory=list)
    eff_dur: float | None = None  # trimmed duration in output; None = use full dur
    freq_hz: int | None = (
        None  # from the WAV's own IC-9700 metadata (read_wav_metadata)
    )
    mode: str | None = None  # ditto
    ptt: bool | None = (
        None  # ditto -- ground truth at the segment's own start, no telemetry lag
    )


def _eff(s: Segment) -> float:
    return s.dur if s.eff_dur is None else s.eff_dur


@dataclass
class Qso:
    dt: datetime  # UTC (from EDI)
    callsign: str
    rst_s: str
    nr_s: str
    rst_r: str
    nr_r: str
    loc: str
    pts: int
    dup: bool
    band: str = ""  # from the EDI PBand header (2M/70CM/23CM); '' if unknown
    mode: str = ""  # SSB/CW/FM, from the EDI per-QSO mode code; '' if unknown


def scan_segments(recdir: str) -> list[Segment]:
    segs: list[Segment] = []
    audio_t = 0.0
    files = sorted(f for f in os.listdir(recdir) if f.lower().endswith(".wav"))
    for f in files:
        try:
            wall = datetime.strptime(f[:15], "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        p = os.path.join(recdir, f)
        w = wave.open(p)
        dur = w.getnframes() / w.getframerate()
        w.close()
        segs.append(Segment(p, wall, dur, audio_t))
        audio_t += dur
    return segs


def read_wav_metadata(segs: list[Segment]) -> None:
    """Populate freq_hz/mode/ptt on each segment straight from its own WAV
    file's embedded IC-9700 metadata. Leaves them None for a file with no
    recognized tag -- no fallback heuristic, since there's nothing to
    fall back to that's as trustworthy (see build_state_events)."""
    for s in segs:
        title = read_wav_title(s.path)
        parsed = parse_wav_title(title) if title else None
        if parsed:
            s.freq_hz, s.mode, s.ptt = parsed


GAP_KEEP_S = 3.0  # seconds kept from each silent gap when --skip-gaps is used


def remap_audio_t(segs: list[Segment], long_cw_segs: set[int] | None = None) -> None:
    """Shorten gap segments to GAP_KEEP_S and recompute audio_t for all segments.

    A gap segment is one with no trusted decoded events and a duration longer
    than MAX_OVER_S — i.e. a listening / calling-CQ stretch between QSOs.
    Call this *after* gate_events has been applied to s.events.

    `long_cw_segs` (a set of `id(seg)`, from the segments decode_long_segment
    recovered content from) marks segments that are long for this reason but
    still carry real recovered CW content -- these must not be trimmed to
    GAP_KEEP_S, or concat_audio's outpoint would cut the very audio just
    decoded out of the rendered output entirely, even though the ticker
    still expects to show its text.
    """
    long_cw_segs = long_cw_segs or set()
    t = 0.0
    for s in segs:
        s.audio_t = t
        if not s.events and s.dur > MAX_OVER_S and id(s) not in long_cw_segs:
            s.eff_dur = GAP_KEEP_S
        t += _eff(s)


def trim_to_duration(segs: list[Segment], max_dur: float) -> list[Segment]:
    """Keep only the segments needed to cover the first max_dur seconds of
    real round time (a --duration preview), shortening the last one to
    land exactly on the cutoff.

    Called *before* CW decoding, not after: decode_segment/gate_events are
    the expensive part of the pipeline, and a short preview has no use for
    segments past the cutoff, so this skips decoding them at all rather than
    decoding the full round and discarding most of the result.
    """
    out = [s for s in segs if s.audio_t < max_dur]
    if out:
        last = out[-1]
        cut = max(0.0, min(_eff(last), max_dur - last.audio_t))
        if cut < _eff(last):
            last.eff_dur = cut
    return out


# Reverse of puskas_logger's own EDI encodings (_BAND_FREQ / _MODE_CODE), so
# a rendered chapter/caption can name the band+mode the logger recorded.


def parse_edi(path: str) -> tuple[str, str, list[Qso]]:
    log = edi.read(path)
    qsos = [
        Qso(
            r.dt,
            r.callsign,
            r.rst_s,
            r.nr_s,
            r.rst_r,
            r.nr_r,
            r.loc,
            r.points,
            r.duplicate,
            band=log.band,
            mode=r.mode,
        )
        for r in log.records
    ]
    return log.my_callsign, log.my_locator, qsos


def merge_edi(paths: list[str]) -> tuple[str, str, list[Qso]]:
    """Merge one or more per-band EDI logs (e.g. 2M + 70CM from the same
    round) into a single chronological QSO list -- the recording is one
    continuous audio timeline regardless of how many bands were worked."""
    my_callsign, mywwl = "", ""
    qsos: list[Qso] = []
    for path in paths:
        mc, mw, qs = parse_edi(path)
        if not my_callsign:
            my_callsign, mywwl = mc, mw
        qsos.extend(qs)
    qsos.sort(key=lambda q: q.dt)
    return my_callsign, mywwl, qsos


def audio_time_for(wall: datetime, segs: list[Segment]) -> float:
    """Map a local wall-clock time to a position in the output video."""
    for s in segs:
        if wall < s.wall:
            return s.audio_t
        if wall < s.wall + timedelta(seconds=s.dur):
            offset = min((wall - s.wall).total_seconds(), _eff(s))
            return s.audio_t + offset
    return segs[-1].audio_t + _eff(segs[-1])


def stream_start(wall: datetime, segs: list[Segment]) -> float:
    """Where a side stream's own frame 0 belongs in the output timeline.

    Same as audio_time_for once the recording has started, but *negative*
    when the stream began before the first WAV segment -- audio_time_for
    clamps that case to segs[0].audio_t, which is exactly wrong here. A cast
    or scope recording clamped to 0 gets its frame 0 pinned to video t=0, so
    everything in it reads late by the whole cast-to-WAV gap (25 s in the
    dry-run that caught it, as the cast PiP's clock lagging the round).

    This is the normal case, not an edge case: run-recorded-contest-session.sh
    starts asciinema before the radio recorder is switched on, so every cast
    made through the documented entrypoint begins ahead of the audio.
    render() turns the negative value into an -ss seek *into* the stream."""
    if wall < segs[0].wall:
        return segs[0].audio_t - (segs[0].wall - wall).total_seconds()
    return audio_time_for(wall, segs)


def derive_utc_offset(segs: list[Segment], qsos: list[Qso]) -> int:
    """Integer-hour offset such that qso_utc + offset ~= wav local time."""
    if not qsos:
        return 0
    wav_mid = segs[0].wall + timedelta(seconds=(segs[-1].audio_t + segs[-1].dur) / 2)
    qso_mid = qsos[0].dt + (qsos[-1].dt - qsos[0].dt) / 2
    return round((wav_mid - qso_mid).total_seconds() / 3600)


_WEBCAM_TS_RE = re.compile(r"(\d{8}_\d{6})")


def parse_webcam_wall(path: str) -> datetime:
    """Parse a phone/webcam filename's embedded timestamp (e.g.
    VID_20260706_180003.mp4) the same way scan_segments reads WAV filenames."""
    m = _WEBCAM_TS_RE.search(os.path.basename(path))
    if not m:
        raise ValueError(f"no YYYYMMDD_HHMMSS timestamp found in {path}")
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")


_WEBCAM_PRECISE_RE = re.compile(r"-webcam-(\d{8}T\d{6}\.\d+Z)\.")


def parse_webcam_precise_filename(path: str) -> datetime | None:
    """Parse the exact, µs-precise UTC timestamp puskas_logger._webcam_toggle
    bakes into the filename on stop (e.g.
    `foo-webcam-20260722T121101.868307Z.mp4`), read from the ffmpeg capture
    log's own frame-0 wallclock at the time (see _webcam_precise_start in
    puskas_logger.py). Preferred over webcam_start_from_log/webcam_start_wall
    below: same precision, but self-contained in the filename itself -- no
    dependency on the sidecar `.log` file surviving alongside the video, and
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
    decode_long_segment's neighbour docstrings), and not something a single
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


# ---------------------------------------------------------------------------
# Rig/rotator state. ptt/freq_hz/mode at a segment's own start come from the
# WAV file's own embedded IC-9700 metadata (read_wav_metadata) -- ground
# truth straight from the rig, with none of a 1 Hz poll's lag. Telemetry
# (puskas_logger's *-telemetry.jsonl) is still used for freq_hz/mode drift
# *within* a long segment (see build_state_events), and for az, which has
# no equivalent in the WAV metadata at all.
# ---------------------------------------------------------------------------


@dataclass
class TelemetrySample:
    t: datetime
    freq_hz: int | None
    mode: str | None
    az: float | None
    # Raw 0-255 meter readings, converted only here at render time -- see the
    # meter curves below for why the logger records them uncalibrated.
    vd: int | None = None
    id_raw: int | None = None
    swr: int | None = None
    po: int | None = None
    # As with az_offline: an absent "vd" key and an explicit `"vd": null` both
    # land as None but mean opposite things -- a line that says nothing about
    # the meters, versus one reporting that the radio went away.
    meters_offline: bool = False
    # An absent "az" key and an explicit `"az": null` both land as az=None but
    # mean opposite things -- silence about the rotator (a rig event) versus a
    # report that it went offline. Only the latter ends az's carry-forward.
    az_offline: bool = False


@dataclass
class SegState:
    ptt: bool | None = None
    freq_hz: int | None = None
    mode: str | None = None


def _parse_telemetry_time(s: str) -> datetime:
    """Both stamp precisions the format has carried: whole seconds (the
    original 1 Hz sampler) and microseconds (the current change-driven
    writer, matching the input log). Raises ValueError on anything else,
    which load_telemetry treats as a bad line."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized telemetry timestamp: {s!r}")


def load_telemetry(path: str) -> list[TelemetrySample]:
    """Parse a puskas_logger `*-telemetry.jsonl` file.

    Records are partial: the rig's own push events carry freq_hz/mode with
    no az, the rotator poller's carry az alone. A missing key is simply
    "this event says nothing about that field" -- build_state_events carries
    each field forward across the events that don't mention it."""
    samples: list[TelemetrySample] = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = _parse_telemetry_time(rec["t"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        samples.append(
            TelemetrySample(
                ts,
                rec.get("freq_hz"),
                rec.get("mode"),
                rec.get("az"),
                az_offline="az" in rec and rec["az"] is None,
                meters_offline="vd" in rec and rec["vd"] is None,
                vd=rec.get("vd"),
                id_raw=rec.get("id"),
                swr=rec.get("swr"),
                po=rec.get("po"),
            )
        )
    return samples


@dataclass
class InputLogEvent:
    t: datetime
    kind: str  # 'text' (keystroke) or 'qso' (an actual submit)
    text: str = ""  # kind == 'text': the full input-box contents
    callsign: str = ""  # kind == 'qso'
    dup: bool = False  # kind == 'qso'


def load_input_log(path: str) -> list[InputLogEvent]:
    """Parse a puskas_logger `*-input.jsonl` log. Two event kinds share the
    file (see puskas_logger.py's own comment on why): 'text' is one line per
    keystroke feeding the typewriter overlay, microsecond-precise but with
    no reliable way to tell a submit from an abort. 'qso' is one line per
    QSO actually appended to the log, written from the one place that
    unambiguously knows -- see match_qso_times, which uses it to give QSO
    panels an exact submit time instead of the EDI's minute-precision guess."""
    out: list[InputLogEvent] = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%S.%fZ")
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        kind = rec.get("event", "text")
        out.append(
            InputLogEvent(
                ts,
                kind,
                rec.get("text", ""),
                rec.get("call", ""),
                rec.get("dup", False),
            )
        )
    return out


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


_LOG_INPUT_RE = re.compile(r"^Input #\d+, ([^,]+)")
_LOG_START_RE = re.compile(r"start:\s*([0-9]+\.[0-9]+)")


def webcam_start_from_log(log_path: str) -> datetime | None:
    """Precise UTC frame-0 wallclock of a logger-recorded webcam, read from
    the ffmpeg capture log (`*-webcam.log`) written next to the mp4.

    ffmpeg prints each input's own capture start time under its `Input #N`
    header. With -use_wallclock_as_timestamps 1 (see
    puskas_logger._webcam_capture_cmd) the v4l2 video input's `start:` is a
    true Unix epoch -- the exact real-world instant of the first frame, to the
    microsecond. Without the flag it's CLOCK_MONOTONIC (uptime), useless as an
    absolute time; the PulseAudio input always reports a wallclock epoch, so we
    prefer the video input's `start:` (it's what the PiP shows) and fall back
    to audio, distinguishing the two by magnitude (a Unix epoch is > 1e9; an
    uptime is far smaller). Returns None if neither is an absolute epoch (an
    old recording without the flag and, implausibly, no usable audio start) or
    the log can't be read.

    This is exact where webcam_start_wall (the logged webcam_start event) is
    stamped ~1s early -- before ffmpeg spawns -- so it takes precedence."""
    video_epoch = audio_epoch = None
    cur = None
    try:
        for line in open(log_path, encoding="utf-8", errors="replace"):
            m = _LOG_INPUT_RE.match(line.strip())
            if m:
                cur = m.group(1)
                continue
            if cur and "start:" in line:
                sm = _LOG_START_RE.search(line)
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


FREQ_MATCH_TOLERANCE_HZ = 500  # see build_state_events' docstring


def build_state_events(
    segs: list[Segment], telemetry: list[TelemetrySample], offset_h: int
) -> list[tuple[float, float, SegState]]:
    """RX/TX + QRG/mode events, one per stretch those stay constant.

    ptt/freq_hz/mode at a segment's own start come straight from
    `Segment.ptt`/`.freq_hz`/`.mode` (read_wav_metadata) -- the WAV file's
    own embedded IC-9700 recorder metadata, ground truth from the rig at
    the exact instant it started recording, with none of a 1 Hz telemetry
    poll's lag. A segment with no such metadata (rare -- e.g. a non-IC-9700
    recording) is skipped entirely rather than guessed at.

    ptt never needs telemetry at all: unlike freq/mode it cannot
    legitimately change mid-segment -- a real transition is exactly what
    causes the recorder to cut a new WAV file -- so it's one value,
    `s.ptt`, for the whole segment. (An earlier version tried to derive
    ptt from telemetry, including a "last sample wins" fix for telemetry's
    own polling lag -- all now unnecessary and removed, since the WAV
    metadata has no lag to correct for in the first place.)

    freq_hz/mode still benefit from telemetry, though: a long segment with
    no PTT activity at all (minutes of listening/tuning between overs) can
    still see the operator QSY with nothing to split the WAV on, so the
    WAV's own metadata (fixed at file-creation time) only captures the
    *starting* frequency/mode. Telemetry sub-divides the segment wherever a
    later sample shows them actually changing -- seeded from the WAV's
    starting value, not from telemetry, so a segment with no telemetry
    change at all just keeps the WAV-sourced value for its whole span.

    Azimuth is deliberately *not* here, though it used to be: a run is
    whatever stretch freq/mode hold for, which can be minutes, and one
    number for all of it (the median of its samples) left the compass
    needle standing still through a real slew and then jumping at the run
    boundary. It is its own time series now -- see hud_az_marks.

    Comparing the two frequency sources exactly (Hz for Hz) is unsound:
    the WAV metadata and rigctld-via-telemetry don't agree to the exact
    Hz even when nothing changed. Checked against this real round's own
    data: a systematic disagreement of 160/250/300/310 Hz (depending on
    band) shows up on *every* segment's very first telemetry sample, which
    would otherwise look like a spurious retune right at the start of
    almost every segment. Genuine retunes in the same data are >=1000 Hz
    (mostly round kHz steps, as a human tuning by hand would produce) --
    a clean gap, zero occurrences between 310 Hz and 1000 Hz -- so
    FREQ_MATCH_TOLERANCE_HZ=500 safely separates "same frequency, two
    slightly disagreeing sources" from "the operator actually retuned"."""
    events: list[tuple[float, float, SegState]] = []
    for s in segs:
        if s.ptt is None and s.freq_hz is None and s.mode is None:
            continue

        utc_start = s.wall - timedelta(hours=offset_h)
        utc_end = utc_start + timedelta(seconds=s.dur)
        inside = sorted(
            (t for t in telemetry if utc_start <= t.t < utc_end), key=lambda t: t.t
        )

        # Runs of consecutive (freq_hz, mode), seeded from the WAV's own
        # metadata, not from telemetry -- only sub-divided when a later
        # telemetry sample shows a genuine change within the segment
        # (frequency beyond FREQ_MATCH_TOLERANCE_HZ, mode by exact string
        # match -- mode has no equivalent rounding-disagreement problem).
        runs: list[tuple[tuple, list[TelemetrySample]]] = [((s.freq_hz, s.mode), [])]
        cur_freq, cur_mode = s.freq_hz, s.mode
        for t in inside:
            new_freq = t.freq_hz if t.freq_hz is not None else cur_freq
            new_mode = t.mode if t.mode is not None else cur_mode
            freq_changed = (
                new_freq is not None
                and cur_freq is not None
                and abs(new_freq - cur_freq) > FREQ_MATCH_TOLERANCE_HZ
            )
            mode_changed = new_mode is not None and new_mode != cur_mode
            if freq_changed or mode_changed:
                cur_freq, cur_mode = new_freq, new_mode
                runs.append(((cur_freq, cur_mode), [t]))
            else:
                runs[-1][1].append(t)

        seg_end = s.audio_t + _eff(s)
        for i, (key, samples) in enumerate(runs):
            start = (
                s.audio_t
                if i == 0
                else audio_time_for(samples[0].t + timedelta(hours=offset_h), segs)
            )
            end = (
                audio_time_for(runs[i + 1][1][0].t + timedelta(hours=offset_h), segs)
                if i + 1 < len(runs)
                else seg_end
            )
            if end <= start:
                continue
            freq_hz, mode = key
            events.append((start, end, SegState(ptt=s.ptt, freq_hz=freq_hz, mode=mode)))
    return events


def match_qso_times(
    qsos: list[Qso], input_log: list[InputLogEvent]
) -> list[datetime | None]:
    """Precise submit timestamp for each qsos[i], from the input log's 'qso'
    events -- an exact replacement for the EDI's minute-precision q.dt when
    available, None otherwise (older recordings, or a --duration cut that
    excludes the matching event).

    Matched by call, in chronological order *within that call* -- deliberately
    not by time, even though the two normally agree exactly (puskas_logger
    derives `q.dt` and the event's own microsecond stamp from one captured
    `now`, so the former is the latter's minute-truncation). Time matching
    breaks the moment they don't: a hand-written or edited log whose timestamp
    crosses a minute boundary silently matches nothing. Call+order has no such
    trap -- a --duration cut only ever removes a *suffix* in time, so the
    surviving occurrences of any call are still a prefix of the full sequence,
    and "next unused" stays correct."""
    by_callsign: dict[str, list[datetime]] = {}
    for e in input_log:
        if e.kind == "qso":
            by_callsign.setdefault(e.callsign, []).append(e.t)
    used: dict[str, int] = {}
    out: list[datetime | None] = []
    for q in qsos:
        i = used.get(q.callsign, 0)
        cands = by_callsign.get(q.callsign, [])
        if i < len(cands):
            out.append(cands[i])
            used[q.callsign] = i + 1
        else:
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# ASS generation
# ---------------------------------------------------------------------------

RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720)}


def _bursts(segs: list[Segment]) -> list[list[Segment]]:
    """Group into maximal runs of consecutive real-over segments (dur <=
    MAX_OVER_S), separated by genuine listening gaps."""
    groups: list[list[Segment]] = []
    cur: list[Segment] = []
    for s in segs:
        if s.dur <= MAX_OVER_S:
            cur.append(s)
        else:
            if cur:
                groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _tx_start(burst: list[Segment]) -> float:
    """Where a QSO actually starts within a burst: the operator's own first
    TX, not necessarily the burst's first segment.

    Without PTT telemetry there's no ground truth for which segments are
    RX vs TX, but two things reliably hold: RX and TX strictly alternate
    (the recorder splits on every switch), and a TX segment -- a brief call
    or report -- is consistently shorter than the RX segment either side of
    it (listening for a reply). So whichever alternating phase (even or odd
    position in the burst) has the shorter median duration is TX, and its
    first occurrence is where this exchange really begins.

    This breaks down while calling CQ: a long stretch of repeated brief TX
    calls with only short listening gaps between them has no single
    "real" start to find this way, and an earlier fruitless call can look
    identical to the one that finally got answered. There's no fix for that
    here -- falls back to the burst's own first segment when the two phases
    aren't distinguishable (fewer than one of each, or equal medians)."""
    if len(burst) < 2:
        return burst[0].audio_t
    even = [s.dur for s in burst[0::2]]
    odd = [s.dur for s in burst[1::2]]
    if not even or not odd:
        return burst[0].audio_t
    even_med, odd_med = statistics.median(even), statistics.median(odd)
    if even_med == odd_med:
        return burst[0].audio_t
    tx_is_even = even_med < odd_med
    for i, s in enumerate(burst):
        if (i % 2 == 0) == tx_is_even:
            return s.audio_t
    return burst[0].audio_t  # unreachable: one phase is always non-empty


def cluster_starts(segs: list[Segment]) -> list[float]:
    """audio_t of the real start of every fresh burst of on-air activity --
    see `_bursts` for how a burst is delimited and `_tx_start` for how its
    real (TX-initiated) start is found within it.

    Deliberately keyed on duration alone, not on whether CW was actually
    decoded (`s.events`): a WAV segment boundary is a precise real-world
    RX/TX transition regardless of what's being transmitted. A voice-mode
    QSO never carries decodable CW, so requiring events made this blind to
    every voice over -- on a mostly-voice recording almost no QSO got the
    audio-precise snap at all. This is pure audio structure, independent of
    both CW content and the EDI log's minute-only timestamp precision."""
    return [_tx_start(b) for b in _bursts(segs)]


def _snap_to_cluster(t: float, clusters: list[float]) -> float:
    """The real activity-burst that produced the EDI-derived approximate
    time `t`. A QSO's own over necessarily starts at or before its (possibly
    minute-truncated) logged completion time, so this is the *latest*
    cluster start <= t -- not simply the nearest one, which can jump ahead
    to the *next* contact's burst if the current QSO took a while (calling,
    retries) to complete before being logged.

    If no cluster is <= t -- e.g. a QSO logged before any CW was ever
    decoded, common on a mostly-voice recording, or simply the first QSO --
    there is nothing to snap to, so `t` itself is used as-is. Falling back to
    the *first* cluster in the whole recording here was a real bug: it could
    pull an early QSO's panel minutes into the future."""
    candidates = [c for c in clusters if c <= t]
    return max(candidates) if candidates else t


def qso_windows(
    qsos: list[Qso],
    segs: list[Segment],
    offset_h: int,
    total: float,
    qso_times: list[datetime | None] | None = None,
) -> list[tuple[float, float]]:
    """Return the (start, end) video-time window shown for each QSO's panel.

    Only the *start* needs a heuristic at all: there's no way to know from
    the EDI or the input log exactly when a real over began, so it's
    snapped onto the actual WAV segment/burst boundary (see cluster_starts)
    nearest the QSO's own approximate time. The *end* doesn't need
    guessing wherever qso_times (from match_qso_times) has an exact
    submit time for that QSO -- that moment (the operator hitting Enter)
    is exact ground truth for when the QSO was done, so the panel simply
    clears there instead of lingering until the next QSO's own panel
    starts (the old behaviour, still used as a fallback when qso_times
    isn't available for a given QSO -- no better information exists then).

    qso_times also still feeds the *start* side, same as before: as the
    anchor into _snap_to_cluster in place of the EDI's minute-precision
    q.dt, which removes the minute-level slop that could otherwise point
    the snap at the wrong neighbouring burst.

    Two (or more) QSOs worked with no real listening gap between them --
    e.g. the same station on SSB then CW then FM in one continuous
    exchange -- are one burst as far as cluster_starts is concerned, since
    there's no audio structure to tell their overs apart at all. A QSO
    that snaps to the *same* cluster as the previous QSO instead starts
    exactly where the previous QSO's own window ended (its real, known
    finish) -- not audio-structure-precise either, but real, and
    critically leaves no overlap and no gap between the two."""
    clusters = cluster_starts(segs)
    starts: list[float] = []
    finishes: list[float | None] = []
    prev_cluster: float | None = None
    for i, q in enumerate(qsos):
        precise = qso_times[i] if qso_times else None
        anchor = precise if precise is not None else q.dt
        anchor_t = audio_time_for(anchor + timedelta(hours=offset_h), segs)
        snapped = _snap_to_cluster(anchor_t, clusters)
        if (
            precise is not None
            and snapped == prev_cluster
            and finishes[i - 1] is not None
        ):
            starts.append(finishes[i - 1])
        else:
            starts.append(snapped)
        finishes.append(anchor_t if precise is not None else None)
        prev_cluster = snapped
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])  # keep panel order sane
    windows: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        fallback_end = starts[i + 1] if i + 1 < len(starts) else total
        end = finishes[i] if finishes[i] is not None else fallback_end
        windows.append((max(0.0, start), max(start + 1.0, end)))
    return windows


def _mode_at(t: float, state_events: list[tuple[float, float, SegState]]) -> str | None:
    for start, end, st in state_events:
        if start <= t < end:
            return st.mode
    return None


def ticker_chunks(
    segs: list[Segment],
    state_events: list[tuple[float, float, SegState]] | None,
    long_cw_spans: list[tuple[float, float, list[CharEvent]]] | None,
) -> list[tuple[float, float, list[CharEvent]]]:
    """Trusted CW content as one chronological list of (start, end, events).

    Two sources merge here: segments decoded whole (dur <= MAX_OVER_S, one
    chunk each), and telemetry-confirmed CW sub-ranges recovered from an
    otherwise-too-long segment we only listened to (see decode_long_segment)
    -- possibly several per segment, since we may have followed more than one
    on-air exchange without ever transmitting ourselves. Segments telemetry
    confirms were *not* CW are skipped outright: the decoder runs blind on
    every segment (there's no way to know the mode in advance) and gate_events
    rejects most non-CW noise, but a strong tone in voice audio can
    occasionally still slip through trusted -- telemetry's own mode is ground
    truth where we have it."""
    chunks: list[tuple[float, float, list[CharEvent]]] = []
    for s in segs:
        mode = _mode_at(s.audio_t, state_events) if state_events is not None else None
        if s.events and (mode is None or mode == "CW"):
            chunks.append((s.audio_t, s.audio_t + _eff(s), s.events))
    chunks.extend(long_cw_spans or [])
    chunks.sort(key=lambda c: c[0])
    return chunks


def ticker_stream(
    chunks: list[tuple[float, float, list[CharEvent]]],
) -> list[tuple[float, str]]:
    """(absolute video time, character) for every decoded character.

    There is no flush marker and no separator inserted between overs. Both
    used to exist because the ticker held a static transcript that had to be
    cleared before it went stale; the display now scrolls on a clock (see
    HudTimeline.at), so a gap between overs *is* a gap on screen and text from
    an earlier burst has physically left the display long before a later one
    arrives. Time does both jobs."""
    stream: list[tuple[float, str]] = []
    for start, _, events in chunks:
        for e in events:
            stream.append((start + e.t, e.ch))
    return stream


# ---------------------------------------------------------------------------
# YouTube chapters + SRT captions (for seeking without scrubbing)
# ---------------------------------------------------------------------------

MIN_CHAPTER_GAP_S = 10  # YouTube ignores chapters closer together than this
CAPTION_DUR_S = 8.0  # how long each SRT cue is shown


def _yt_time(t: float) -> str:
    """Format seconds as a YouTube description timestamp (M:SS or H:MM:SS)."""
    t = int(round(max(0.0, t)))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _qso_label(i: int, q: Qso) -> str:
    """The one-line label shared by chapter markers and SRT cues, so the two
    never drift: 'QSO 001 HA5MIG  2M SSB', band/mode omitted when unknown,
    with a ' (dup)' suffix for duplicates. Deliberately just call + band/mode
    -- locator, distance, serials and reports were dropped from the caption as
    redundant noise (they're already on the logger's own on-screen PiP)."""
    bm = " ".join(x for x in (q.band, q.mode) if x)
    bm = f"  {bm}" if bm else ""
    tag = " (dup)" if q.dup else ""
    return f"QSO {i + 1:03d} {q.callsign}{bm}{tag}"


def build_chapters(qsos: list[Qso], windows: list[tuple[float, float]]) -> str:
    """YouTube description chapter markers, one per QSO (plus the mandatory 0:00).

    YouTube requires the first chapter at 0:00, at least 3 chapters, and each
    at least MIN_CHAPTER_GAP_S apart -- closer QSOs are dropped from the list
    (they still get an SRT cue, just no separate chapter marker).
    """
    lines = ["0:00 Start"]
    last_t = 0
    for i, (q, (start, _end)) in enumerate(zip(qsos, windows)):
        t = int(round(start))
        if t - last_t < MIN_CHAPTER_GAP_S:
            continue
        lines.append(f"{_yt_time(t)} {_qso_label(i, q)}")
        last_t = t
    return "\n".join(lines) + "\n"


def _srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(qsos: list[Qso], windows: list[tuple[float, float]]) -> str:
    """One caption cue per QSO -- gives a clickable transcript in the YouTube
    sidebar, independent of the chapter markers (and of whether CC is on)."""
    blocks = []
    for i, (q, (start, end)) in enumerate(zip(qsos, windows)):
        end = min(end, start + CAPTION_DUR_S)
        text = _qso_label(i, q)
        blocks.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def concat_audio(segs: list[Segment], out_wav: str) -> None:
    listfile = out_wav + ".txt"
    with open(listfile, "w") as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s.path)}'\n")
            if s.eff_dur is not None:
                fh.write(f"outpoint {s.eff_dur:.6f}\n")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listfile,
            "-c",
            "copy",
            out_wav,
        ],
        check=True,
    )
    os.remove(listfile)


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return float(out.strip())


CAST_PIP_X_FRAC = 0.0104  # the terminal PiP is the dominant visual element, not
CAST_PIP_MARGIN_FRAC = 0.015  # a small inset -- the logger UI is most of what
# there is to watch. Its size is height-constrained (see render): with the HUD
# along the bottom, the room above it is the limit.
CAST_PIP_ALPHA = 0.85  # slightly transparent so the waterfall shows
# faintly through the terminal PiP; 1.0 = opaque
STREAM_TRIM_MARGIN_S = 5.0  # slack when trimming a side stream to the cut,
# so tpad's last-frame cloning never shows at the end of a preview
RENDER_FPS = 30  # output frame rate; the webcam PiP is resampled to
# this too (see render) so both branches share one
# real-time clock


# ---------------------------------------------------------------------------
# Scope (spectrum-scope waterfall) background -- from icom_net.py's .scope
# recordings (real IC-9700 CI-V scope sweeps), instead of showspectrum's
# reconstruction from the recorded audio. See icom_net.py's own docs for
# where these come from; this section only renders them into video.
# ---------------------------------------------------------------------------

SCOPE_AMP_MAX = 160  # Icom's own linear scope units, not dBm (see write_scope_record)

SCOPE_WATERFALL_SPAN_S = 10.0  # seconds of history the canvas height represents,
# matching the real IC-9700 display: a signal takes ~4-5s to
# scroll through half the physical waterfall's height there.

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
    rate or where sweeps stop arriving entirely -- the same way a real
    waterfall display behaves when its input momentarily stalls.
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
                    canvas[0] = row
                next_row_t += row_dt
            proc.stdin.write(canvas.tobytes())
            t += frame_dt
    finally:
        proc.stdin.close()
        proc.wait()


# ---------------------------------------------------------------------------
# HUD (DOOM-style status bar)
#
# Split in two on purpose: everything above draw_hud_frame is the *data*
# layer -- pure functions turning the recording's own sources into a
# HudState at any video time -- and everything below is drawing. The data
# layer needs no art, no fonts and no ffmpeg, so it is fully unit-testable;
# the drawing layer's geometry lives in one table (hud_layout) so replacing
# the placeholder background with real artwork means editing coordinates,
# not drawing code.
# ---------------------------------------------------------------------------

HUD_TICKER_CHARS = 15  # cells in the ticker display, set by the artwork's own
# CW slot: 15 cells at a 5px dot pitch fill 445 of its 446 pixels at 1080p, and
# 16 would drop the pitch to 4. The display scrolls, so a short window loses
# nothing -- its value is "something is arriving right now", not a backlog.
HUD_TICKER_SPAN_S = 8.0  # seconds for a character to cross it
HUD_TICKER_BURST_S = 3.0  # gap beyond which the operator has stopped sending,
# not merely paused between characters: the longest single character is ~2 s at
# the slowest speed worked here, and word gaps arrive as their own ' '.
HUD_RATE_WINDOW_S = 600.0  # trailing window behind the QSOs/hour readout
HUD_SCORE_ANIM_S = 0.6  # score count-up + panel flash after each QSO
HUD_S_CENTRE_BINS = 3  # scope bins taken as "the tuned frequency"
HUD_S_HOLD_S = 1.0  # no sweep for this long = no signal reading at all


@dataclass
class HudState:
    """Everything the HUD draws, at one instant of video time."""

    t: float = 0.0
    utc: datetime | None = None
    score: int = 0  # animated: counts up over HUD_SCORE_ANIM_S after a QSO
    score_flash: float = 0.0  # 1.0 right after a QSO, decaying to 0.0
    qsos: int = 0
    rate_per_h: float = 0.0
    best_km: int = 0
    freq_hz: int | None = None
    mode: str | None = None
    band: str | None = None
    ptt: bool | None = None
    rot_az: float | None = None  # where the rotator actually points
    target_az: float | None = None  # bearing to the station being worked
    s_level: float | None = None  # 0..1, from the scope's own centre bins
    # (column offset from the display's left edge, character) for whatever is
    # currently on the scrolling matrix -- not a string, because a character
    # sits at a dot-column position rather than in a slot.
    ticker: list[tuple[int, str]] = field(default_factory=list)
    vd: float | None = None  # volts -- no recording carries these yet; the
    id_a: float | None = None  # panel renders "---" until the logger records them


def hud_qso_marks(
    qsos: list[Qso], windows: list[tuple[float, float]]
) -> list[tuple[float, int, int, int]]:
    """(video_t, cumulative score, cumulative QSO count, best DX km) at the
    moment each QSO completes.

    The mark lands on `windows[i][1]` -- the QSO's *end*, which wherever the
    input log gave an exact submit time is the real instant the operator hit
    Enter (see qso_windows). That is when a score genuinely changes, so it's
    also when the HUD's counter should tick over.

    Best DX comes from q.pts rather than a recomputed distance because that's
    the EDI's own scoring field, which is exactly what the SCORE panel sums.
    A dup scores 0 there, so a dup of a far station never becomes best DX --
    correct for a scoreboard, if not for bragging rights."""
    order = sorted(range(len(qsos)), key=lambda i: windows[i][1])
    marks: list[tuple[float, int, int, int]] = []
    score = best = 0
    for n, i in enumerate(order, start=1):
        score += qsos[i].pts
        best = max(best, qsos[i].pts)
        marks.append((windows[i][1], score, n, best))
    return marks


def hud_target_spans(
    qsos: list[Qso], windows: list[tuple[float, float]], my_loc: str
) -> list[tuple[float, float, float]]:
    """(start, end, bearing) of the station being worked, for the HUD
    compass's second (ghost) needle -- so the rotator needle can be seen
    swinging onto the target. Silently skips a QSO whose locator or our own
    won't parse; there is simply no bearing to show then."""
    me = maidenhead_to_latlon(my_loc)
    if me is None:
        return []
    spans: list[tuple[float, float, float]] = []
    for q, (start, end) in zip(qsos, windows):
        them = maidenhead_to_latlon(q.loc)
        if them is not None:
            spans.append((start, end, initial_bearing(*me, *them)))
    spans.sort(key=lambda s: s[0])
    return spans


def hud_s_marks(
    records: list[tuple[float, int, int, bytes]],
    segs: list[Segment],
    offset_h: int,
    bins: int = HUD_S_CENTRE_BINS,
) -> list[tuple[float, float]]:
    """(video_t, 0..1 signal level) from the scope recording's own centre
    bins -- a genuine S-meter for the HUD that costs no new recording and
    works retroactively on every round captured since the logger's scope
    recorder went in.

    The centre bin really is the tuned frequency: the IC-9700's scope runs in
    Centre mode (see icom_net.parse_scope_frame), and at 475 bins across a
    1 MHz span one bin is ~2.1 kHz -- close enough to an SSB passband that
    this is a real reading rather than a rough proxy. `bins` are taken as a
    max, not a mean, so a signal sitting in one bin isn't diluted by the
    quiet ones either side of it.

    Not the same quantity as CI-V's own S-meter (`15 02`), which is
    post-filter and post-AGC; a live capture confirmed the radio only ever
    reports that when polled, so no existing recording has it."""
    marks: list[tuple[float, float]] = []
    half = max(0, bins // 2)
    for ts, _, _, pixels in records:
        if not pixels:
            continue
        centre = len(pixels) // 2
        window = pixels[max(0, centre - half) : centre + half + 1]
        wall = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
            tzinfo=None
        ) + timedelta(hours=offset_h)
        marks.append(
            (audio_time_for(wall, segs), min(1.0, max(window) / SCOPE_AMP_MAX))
        )
    marks.sort(key=lambda m: m[0])
    return marks


# --- meter calibration ------------------------------------------------------
#
# The logger records raw 0-255 meter readings and conversion happens here, at
# render time, deliberately: this is the least trustworthy data in the whole
# pipeline, and keeping it raw on disk makes a corrected curve a one-line
# change rather than a recording that has to be thrown away.
#
# Vd, SWR and Po use Icom's own published calibration points, and Vd was
# checked against a multimeter on this radio -- raw 152 converts to 13.66 V
# against a measured 13.78 V, 0.9% out. Po's 100% point was confirmed too
# (raw 213 during a full-power transmission).
#
# Id is the exception and is NOT Icom's curve: theirs (0/97/146/241 ->
# 0/10/15/25 A) reads 17.6 A for a raw 171 that measures ~12.8 A of real PA
# drain. Measured directly against a multimeter in series with the supply, at
# raw 55/60/61/62/64 plus a 100%-power anchor at raw 171, PA drain (total
# current less the 1.18 A measured receive baseline) fits a straight line
# *through the origin* at 0.0741 A per raw unit, i.e. ~17.9 A full scale
# rather than Icom's 25 A. The low cluster alone gives 0.0726 and adding the
# 100% anchor gives 0.0741 -- two nearly independent estimates a factor of
# three apart in current, agreeing to 2%, which is what makes the line
# through zero believable rather than merely fitted; the low cluster spans
# only raw 55-64, far too short a lever arm to determine a slope by itself.
# Residuals within +-5.3%, worst at the lowest point, where a cheap meter on
# a 20 A range has its poorest resolution and where the assumption of a
# constant receive baseline is least safe (the meter's own burden had the
# radio down at ~10.2-10.7 V during these readings).
_VD_CURVE = [(0, 0.0), (13, 10.0), (241, 16.0)]
_ID_CURVE = [(0, 0.0), (241, 17.85)]
_SWR_CURVE = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0)]
_PO_CURVE = [(0, 0.0), (143, 50.0), (213, 100.0)]


def _meter_value(curve: list[tuple[int, float]], raw: int | None) -> float | None:
    """Piecewise-linear lookup, extrapolating from the last segment above the
    curve's top point (Icom's own points stop short of full scale)."""
    if raw is None:
        return None
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if raw <= x1 or (x1, y1) == curve[-1]:
            return y0 + (raw - x0) * (y1 - y0) / (x1 - x0)
    return curve[-1][1]


def vd_volts(raw: int | None) -> float | None:
    return _meter_value(_VD_CURVE, raw)


def id_amps(raw: int | None) -> float | None:
    return _meter_value(_ID_CURVE, raw)


def swr_ratio(raw: int | None) -> float | None:
    return _meter_value(_SWR_CURVE, raw)


def po_percent(raw: int | None) -> float | None:
    return _meter_value(_PO_CURVE, raw)


def hud_meter_marks(
    telemetry: list[TelemetrySample], segs: list[Segment], offset_h: int
) -> list[tuple[float, TelemetrySample]]:
    """(video_t, sample) for every telemetry line that carries meter readings.

    Meters are change-only in the recording, like everything else in that
    file, so a mark holds until the next one -- there is no staleness horizon
    the way the scope-derived S-meter has, because an unchanging supply
    voltage is a real reading rather than a gap in the data.

    That is exactly why a radio disconnect has to be marked explicitly rather
    than left as silence: "no reason for the voltage to have changed" and "the
    radio is gone" are indistinguishable from absence alone, and a real
    session dropped three times in nine minutes would otherwise show the
    pre-outage voltage throughout each outage."""
    marks = [
        (audio_time_for(t.t + timedelta(hours=offset_h), segs), t)
        for t in telemetry
        if t.vd is not None or t.id_raw is not None or t.meters_offline
    ]
    marks.sort(key=lambda m: m[0])
    return marks


HUD_AZ_INTERP_S = 2.0  # rotator samples closer than this are one movement


def hud_az_marks(
    telemetry: list[TelemetrySample], segs: list[Segment], offset_h: int
) -> list[tuple[float, float | None]]:
    """(video_t, azimuth) for every telemetry line that reports on the rotator,
    offline ones included -- an explicit `"az": null` is a real mark carrying
    None, so the needle stops there instead of pointing at the last known
    azimuth for the rest of the video. A line that only reports the rig says
    nothing about the rotator and is not a mark at all, even though both load
    as `az=None`.

    The compass reads this directly rather than taking `SegState.az` (a median
    over a freq/mode run) the way the old text badge did: a run can be minutes
    long, so a rotator swung from 250 to 31 degrees over half a minute inside
    one of them collapsed to a single median and the needle stood still, then
    jumped at the run boundary -- seen in the real August round."""
    marks = [
        (audio_time_for(t.t + timedelta(hours=offset_h), segs), t.az)
        for t in telemetry
        if t.az is not None or t.az_offline
    ]
    marks.sort(key=lambda m: m[0])
    return marks


def _az_between(a: float, b: float, frac: float) -> float:
    """Bearing `frac` of the way from a to b, the short way round -- 250 to 31
    degrees is a 141 degree swing clockwise through north, not 219 the other
    way."""
    return (a + ((b - a + 180) % 360 - 180) * frac) % 360


def wall_time_at(
    t: float, segs: list[Segment], starts: list[float] | None = None
) -> datetime | None:
    """Local wall-clock time at video position t -- the inverse of
    audio_time_for. `starts` is an optional precomputed [s.audio_t ...] so a
    caller reading this once per rendered frame doesn't rebuild it every
    time."""
    if not segs:
        return None
    starts = starts if starts is not None else [s.audio_t for s in segs]
    s = segs[max(0, bisect.bisect_right(starts, t) - 1)]
    return s.wall + timedelta(seconds=max(0.0, min(t - s.audio_t, _eff(s))))


@dataclass
class HudTimeline:
    """Precomputed HUD sources, queried per rendered frame by `at()`.

    Every source is stored as a time-sorted list and looked up by bisect
    rather than scanned: a two-hour render asks this ~216,000 times, so a
    linear scan per frame over hundreds of segments or thousands of decoded
    characters would dominate the whole pass."""

    segs: list[Segment]
    offset_h: int = 0
    qso_marks: list[tuple[float, int, int, int]] = field(default_factory=list)
    target_spans: list[tuple[float, float, float]] = field(default_factory=list)
    state_events: list[tuple[float, float, SegState]] = field(default_factory=list)
    az_marks: list[tuple[float, float | None]] = field(default_factory=list)
    s_marks: list[tuple[float, float]] = field(default_factory=list)
    meter_marks: list[tuple[float, TelemetrySample]] = field(default_factory=list)
    stream: list[tuple[float, str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._seg_starts = [s.audio_t for s in self.segs]
        self._qso_t = [m[0] for m in self.qso_marks]
        self._target_t = [s[0] for s in self.target_spans]
        self._state_t = [e[0] for e in self.state_events]
        self._az_t = [m[0] for m in self.az_marks]
        self._s_t = [m[0] for m in self.s_marks]
        self._meter_t = [m[0] for m in self.meter_marks]
        self._ticker_t = [e[0] for e in self.stream]
        # Where each character sits on the strip, in dot columns. Within an
        # over that is exactly one cell after the one before it, whatever the
        # gap in real time: a T is one dit of air time and a 0 nineteen, so
        # placing characters by keying time (which is what this used to do)
        # spaced them raggedly, by fractions of a cell, for no reason a viewer
        # could see. The keying time drives the *scroll* instead -- see
        # _ticker_scroll -- which is where that timing genuinely belongs.
        #
        # Real elapsed time takes over once the gap exceeds HUD_TICKER_BURST_S,
        # by which point the operator has stopped sending rather than paused
        # between characters (word gaps are their own decoded ' ' characters,
        # so they need no room of their own here). That is what still drains
        # the display between overs, and it is why staleness stays
        # structurally impossible.
        self._ticker_cols: list[float] = []
        prev_col, prev_t = 0.0, None
        for t, _ in self.stream:
            if prev_t is None:
                prev_col = t * HUD_TICKER_COLS_PER_S
            elif t - prev_t <= HUD_TICKER_BURST_S:
                prev_col += HUD_TICKER_CELL_COLS
            else:
                prev_col += (t - prev_t) * HUD_TICKER_COLS_PER_S
            prev_t = t
            self._ticker_cols.append(prev_col)

    def _az_at(self, t: float) -> float | None:
        """The rotator's azimuth at t, swept between samples rather than
        stepped to them.

        The poller reports whole degrees about once a second, so a real slew
        arrives as a run of closely-spaced samples: interpolating across gaps
        no longer than HUD_AZ_INTERP_S turns those steps into one continuous
        turn, while a longer gap is not a slow movement at all -- it is the
        rotator sitting still (change-only telemetry writes nothing then), so
        the bearing holds and the next sample is where it moved to."""
        i = bisect.bisect_right(self._az_t, t)
        if not i:
            return None
        az = self.az_marks[i - 1][1]
        if az is None or i >= len(self.az_marks):
            return az
        nxt_t, nxt_az = self.az_marks[i]
        span = nxt_t - self._az_t[i - 1]
        if nxt_az is None or span > HUD_AZ_INTERP_S or span <= 0:
            return az
        return _az_between(az, nxt_az, (t - self._az_t[i - 1]) / span)

    def _ticker_scroll(self, t: float) -> float:
        """How far the ticker's strip has scrolled, in dot columns.

        Each character is pinned: when it was keyed, it had just arrived at the
        right-hand edge. Between two pins the strip moves at whatever rate
        carries it exactly one cell in the real time between them, so the
        display hurries along under fast keying and idles under slow -- which
        is how a fixed inter-character spacing can still show real timing.
        Outside the pins (before the first character, after the last, and
        across the real-time gaps between overs) it runs at the base rate, so
        the display always drains within HUD_TICKER_SPAN_S of the last
        character."""
        i = bisect.bisect_right(self._ticker_t, t)
        if not self._ticker_t:
            return t * HUD_TICKER_COLS_PER_S
        if i == 0 or i == len(self._ticker_t):
            j = max(0, i - 1)
            edge = self._ticker_cols[j] + HUD_TICKER_CELL_COLS
            return edge + (t - self._ticker_t[j]) * HUD_TICKER_COLS_PER_S
        span = self._ticker_t[i] - self._ticker_t[i - 1]
        if span <= 0:
            return self._ticker_cols[i] + HUD_TICKER_CELL_COLS
        frac = (t - self._ticker_t[i - 1]) / span
        moved = self._ticker_cols[i] - self._ticker_cols[i - 1]
        return self._ticker_cols[i - 1] + HUD_TICKER_CELL_COLS + frac * moved

    def at(self, t: float) -> HudState:
        st = HudState(t=t, utc=None)
        wall = wall_time_at(t, self.segs, self._seg_starts)
        if wall is not None:
            st.utc = wall - timedelta(hours=self.offset_h)

        i = bisect.bisect_right(self._qso_t, t)
        if i:
            mark_t, score, n, best = self.qso_marks[i - 1]
            prev = self.qso_marks[i - 2][1] if i >= 2 else 0
            # Count up to the new total rather than snapping to it, and flash
            # the panel over the same window -- DOOM's health readout is the
            # thing the eye goes to, so a QSO landing should be visible.
            phase = min(1.0, (t - mark_t) / HUD_SCORE_ANIM_S) if HUD_SCORE_ANIM_S else 1
            st.score = round(prev + (score - prev) * phase)
            st.score_flash = max(0.0, 1.0 - phase)
            st.qsos, st.best_km = n, best
        lo = bisect.bisect_right(self._qso_t, t - HUD_RATE_WINDOW_S)
        st.rate_per_h = (i - lo) * 3600.0 / HUD_RATE_WINDOW_S

        j = bisect.bisect_right(self._target_t, t)
        if j and t < self.target_spans[j - 1][1]:
            st.target_az = self.target_spans[j - 1][2]

        k = bisect.bisect_right(self._state_t, t)
        if k and t < self.state_events[k - 1][1]:
            seg_state = self.state_events[k - 1][2]
            st.ptt, st.mode = seg_state.ptt, seg_state.mode
            st.freq_hz = seg_state.freq_hz
            if st.freq_hz:
                st.band = band_from_hz(st.freq_hz)

        st.rot_az = self._az_at(t)

        m = bisect.bisect_right(self._s_t, t)
        if m and t - self.s_marks[m - 1][0] <= HUD_S_HOLD_S:
            st.s_level = self.s_marks[m - 1][1]

        q = bisect.bisect_right(self._meter_t, t)
        if q:
            sample = self.meter_marks[q - 1][1]
            st.vd = vd_volts(sample.vd)
            st.id_a = id_amps(sample.id_raw)

        # Everything still on the display: a character enters at the right
        # edge when the scroll reaches its own column and leaves on the left
        # HUD_TICKER_SPAN_S later, with no clearing rule needed -- staleness
        # is structurally impossible rather than guarded against.
        width = HUD_TICKER_CHARS * HUD_TICKER_CELL_COLS
        scroll = self._ticker_scroll(t)
        p = bisect.bisect_right(self._ticker_t, t)
        for i in range(p - 1, -1, -1):
            offset = round(self._ticker_cols[i] - scroll) + width
            if offset <= -HUD_MATRIX_COLS:
                break
            if offset < width:
                st.ticker.append((offset, self.stream[i][1]))
        st.ticker.reverse()
        return st


def build_hud_timeline(
    segs: list[Segment],
    qsos: list[Qso],
    windows: list[tuple[float, float]],
    my_loc: str,
    offset_h: int,
    state_events: list[tuple[float, float, SegState]] | None = None,
    scope_records: list[tuple[float, int, int, bytes]] | None = None,
    long_cw_spans: list[tuple[float, float, list[CharEvent]]] | None = None,
    telemetry: list[TelemetrySample] | None = None,
) -> HudTimeline:
    return HudTimeline(
        segs=segs,
        offset_h=offset_h,
        qso_marks=hud_qso_marks(qsos, windows),
        target_spans=hud_target_spans(qsos, windows, my_loc),
        state_events=state_events or [],
        az_marks=hud_az_marks(telemetry or [], segs, offset_h),
        s_marks=hud_s_marks(scope_records or [], segs, offset_h),
        meter_marks=hud_meter_marks(telemetry or [], segs, offset_h),
        stream=ticker_stream(ticker_chunks(segs, state_events, long_cw_spans)),
    )


# --- the artwork: theme file, sprites, prepared layout -----------------------
#
# The bar *is* the finished artwork (hud-theme/). It carries every panel,
# recess, static label and the compass rose, so nothing static is drawn here --
# drawing a label the artwork already bakes would simply print it twice. What
# this file draws is only what changes: the readouts, five sprites, and the
# dimming of whatever is not currently selected.
#
# theme.json holds every coordinate in *artwork pixels*, hand-verified against
# the image (see --hud-theme-check). Coordinates as data rather than as source
# is what makes the artwork replaceable: new art means a new theme.json, not a
# code change. Auto-detection off the artwork's own pixels finds the
# high-contrast recesses and the magenta-keyed sprites reliably, but a recess
# whose interior is close in brightness to the panel around it cannot be
# separated from it, so those were read by hand -- which is why the check tool
# exists at all.
#
# Script-relative, not CWD-relative: contest renders are run from a contest
# directory (`cd 26augusztus && uv run ../contest_video.py ...`), where a
# relative "hud-theme" would not exist.
HUD_THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud-theme")
_THEME_OUTLINES = {  # group -> colour, matching what the overlay draws
    "slots": (0, 255, 255),
    "chips": (255, 140, 0),
    "stats": (255, 255, 0),
    "sprites": (0, 255, 0),
}


def load_hud_theme(path: str = HUD_THEME_DIR) -> dict:
    """Read a theme directory into {..., 'image': PIL.Image}."""
    with open(os.path.join(path, "theme.json")) as fh:
        theme = json.load(fh)
    theme["image"] = Image.open(
        os.path.join(path, theme.get("artwork", "artwork.png"))
    ).convert("RGB")
    return theme


def hud_theme_rects(theme: dict) -> list[tuple[str, tuple, tuple]]:
    """(group, colour, rect) for everything theme.json positions, flattened."""
    out = []
    for name, rect in theme.get("slots", {}).items():
        out.append((f"slots.{name}", _THEME_OUTLINES["slots"], tuple(rect)))
    for row, rects in theme.get("chips", {}).items():
        for i, rect in enumerate(rects):
            out.append((f"chips.{row}[{i}]", _THEME_OUTLINES["chips"], tuple(rect)))
    for i, rect in enumerate(theme.get("stats", [])):
        out.append((f"stats[{i}]", _THEME_OUTLINES["stats"], tuple(rect)))
    for name, sp in theme.get("sprites", {}).items():
        out.append((f"sprites.{name}", _THEME_OUTLINES["sprites"], tuple(sp["box"])))
    return out


def hud_theme_overlay(theme: dict) -> Image.Image:
    """The artwork with every rect in theme.json drawn onto it, and each
    needle's pivot marked -- the check for a hand-edited theme."""
    img = theme["image"].copy()
    draw = ImageDraw.Draw(img)
    bar = theme.get("bar")
    if bar:
        draw.rectangle(
            [bar[0], bar[1], bar[0] + bar[2] - 1, bar[1] + bar[3] - 1],
            outline=(255, 0, 255),
            width=3,
        )
    for name, colour, (x, y, w, h) in hud_theme_rects(theme):
        draw.rectangle([x, y, x + w, y + h], outline=colour, width=3)
        draw.text((x + 3, y + 3), name.split(".")[-1], fill=colour)
    for sp in theme.get("sprites", {}).values():
        if "pivot" in sp:
            px, py = sp["pivot"]
            draw.ellipse([px - 8, py - 8, px + 8, py + 8], outline=(255, 0, 0), width=3)
            draw.line([px - 12, py, px + 12, py], fill=(255, 0, 0), width=1)
            draw.line([px, py - 12, px, py + 12], fill=(255, 0, 0), width=1)
    return img


# The bar is drawn at the artwork's own aspect (1982x351 = 5.65:1) and never at
# any other: it is scaled uniformly or not at all, since squashing it to a
# different aspect turns the compass into an ellipse -- and the compass is the
# specific reason this artwork was chosen. 340px of a 1080p frame is 31% of its
# height, which the cast PiP (height-constrained when a HUD is present, see
# render) absorbs by shrinking.
HUD_W, HUD_H = 1920, 340
HUD_RED = (255, 48, 32)
HUD_AMBER = (255, 176, 32)
HUD_GREEN = (72, 255, 96)

_HUD_BANDS = ("2M", "70CM", "23CM")
_HUD_MODES = ("SSB", "CW", "FM")
# The artwork bakes the band/mode chips and the whole S-meter *lit*; whatever
# is not currently selected (or not currently reading) is dimmed in place
# rather than being a second, unlit asset that would have to be kept
# stylistically in sync with the lit one.
HUD_UNLIT_DIM = 0.15
HUD_SLOT_PAD = 0.06  # margin inside a recess, as a fraction of its short side
HUD_METER_SEGMENTS = 21  # LEDs in the meter sprite, counted off the artwork
# How far a needle reaches, as a fraction of the compass slot's radius. The
# sprites are not drawn to the rose's own scale (at 1:1 they overshoot the
# compass card entirely), so this is a fit, not a measurement: it lands the tip
# just inside the ring of N/E/S/W letters.
HUD_NEEDLE_FRAC = 0.75
# Physical cell counts, as a real instrument's display would have. A leading
# "1" is a half digit: the cell can only ever show a 1, which is what the unlit
# backdrop then advertises. Sized from real results -- the best single-round
# score seen in published Puskas logs is 8937, and QSO counts run to a few
# dozen -- so 4.5 digits of score (19999) and 2.5 of QSOs (199) have room to
# spare without wasting cells that would shrink every digit.
HUD_SCORE_FIELD = "18888"
HUD_QSOS_FIELD = "188"
# The QRG is fixed-width for a different reason: 23cm is 1296.174, one cell
# wider than 2m's 144.174, so without a field the digits resize on a band
# change mid-video. Its leading cell is a half digit too -- the highest band
# this radio has is 1296 MHz, so a thousands digit above 1 cannot occur.
HUD_QRG_FIELD = "1888.888"


def _key_magenta(img: Image.Image) -> Image.Image:
    """Cut a sprite out of the sheet's flat magenta background.

    The key is a hard threshold on "magenta-ness" (min(R,B) - G), which is
    large only for the background and at most zero for anything the sprites are
    actually made of -- red, orange, green, white highlights and the grey
    pivot ball all have G at least as high as one of R/B. A hard threshold
    rather than a soft alpha ramp because the sheet is not flat #FF00FF in
    practice (it carries generation/compression noise, and only 145 pixels of
    the whole image are exactly the key colour), so the edge pixels a ramp
    would keep semi-opaque are magenta-tinted and would read as a pink fringe.
    Keyed-out pixels are blacked as well as cleared so that resampling blends
    edges toward black -- the sprites already have black outlines, so the
    fringe that leaves is the outline itself."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.minimum(a[:, :, 0], a[:, :, 2]) - a[:, :, 1] > 60
    rgba = np.dstack([np.asarray(img.convert("RGB")), np.where(bg, 0, 255)])
    rgba[bg] = 0
    return Image.fromarray(rgba.astype(np.uint8), "RGBA")


@dataclass
class HudArt:
    """The artwork prepared for one bar size, so a render frame is a copy plus
    the values: the bar image itself, every theme.json rect scaled into it, and
    the sprites cut out, keyed and pre-scaled to where they get pasted.

    Needle pivots are in their own sprite's coordinates -- these needles turn
    about the ball at their base, not about their bounding box's centre."""

    bar: Image.Image
    slots: dict[str, tuple[int, int, int, int]]
    chips: dict[str, list[tuple[int, int, int, int]]]
    stats: list[tuple[int, int, int, int]]
    sprites: dict[str, Image.Image]
    pivots: dict[str, tuple[float, float]]


def hud_art(theme: dict, W: int = HUD_W, H: int = HUD_H) -> HudArt:
    """Scale a theme's artwork and coordinates to a W x H bar."""
    bx, by, bw, bh = theme["bar"]
    sx, sy = W / bw, H / bh

    def scaled(rect):
        x, y, w, h = rect
        return (
            round((x - bx) * sx),
            round((y - by) * sy),
            round(w * sx),
            round(h * sy),
        )

    art = theme["image"]
    bar = art.crop((bx, by, bx + bw, by + bh)).resize((W, H), Image.LANCZOS)
    slots = {name: scaled(r) for name, r in theme["slots"].items()}

    def cut(name: str) -> Image.Image:
        x, y, w, h = theme["sprites"][name]["box"]
        return _key_magenta(art.crop((x, y, x + w, y + h)))

    def fit(name: str, slot: str) -> Image.Image:
        return cut(name).resize(slots[slot][2:], Image.LANCZOS)

    sprites = {"rx": fit("rx", "lamp"), "tx": fit("tx", "lamp")}
    sprites["meter"] = fit("meter", "smeter")
    pivots = {}
    for name in ("needle", "target"):
        sp = theme["sprites"][name]
        px, py = sp["pivot"][0] - sp["box"][0], sp["pivot"][1] - sp["box"][1]
        # py is the sprite's own tip length: the tip sits at its top edge.
        scale = HUD_NEEDLE_FRAC * min(slots["compass"][2:]) / 2 / py
        img = cut(name)
        sprites[name] = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
        pivots[name] = (px * scale, py * scale)
    return HudArt(
        bar=bar,
        slots=slots,
        chips={r: [scaled(c) for c in cs] for r, cs in theme["chips"].items()},
        stats=[scaled(r) for r in theme["stats"]],
        sprites=sprites,
        pivots=pivots,
    )


def _inset(rect, frac: float = HUD_SLOT_PAD) -> tuple[int, int, int, int]:
    """A recess's usable interior: the readout must not sit hard against the
    frame the artwork drew around it."""
    x, y, w, h = rect
    d = round(min(w, h) * frac)
    return (x + d, y + d, w - 2 * d, h - 2 * d)


# Seven-segment digits come from DSEG7 (Debian's fonts-dseg, SIL OFL) rather
# than being drawn as polygons -- an earlier version built each segment by
# hand to avoid a font dependency, but the package is packaged, the glyphs are
# better than hand-rolled ones, and it removed ~120 lines of geometry.
# Unlit segments are drawn too, very dim: that is what makes an LED panel
# read as a panel rather than as numerals floating on black. Keep HUD_SEG_DIM
# low -- at 0.16 the ghost behind a '1' (which lights only its two right-hand
# bars) read as a digit being clipped by the panel edge rather than as an
# unlit cell.
# Vendored beside the artwork rather than taken from /usr/share/fonts: the HUD
# is unrenderable without it, and a system font package is one more thing that
# has to be installed on every machine that renders (CI included).
DSEG_FONT_PATH = os.path.join(HUD_THEME_DIR, "DSEG7Classic-Bold.ttf")
HUD_SEG_DIM = 0.12  # brightness of an unlit segment

_DSEG_FONTS: dict[int, ImageFont.FreeTypeFont] = {}


def _dseg_font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _DSEG_FONTS:
        _DSEG_FONTS[size] = ImageFont.truetype(DSEG_FONT_PATH, size)
    return _DSEG_FONTS[size]


def _all_segments(text: str) -> str:
    """`text` with every segment lit. '.' and ':' have no advance width of
    their own in DSEG7 (they overlay the preceding cell), so keeping them
    keeps the lit and unlit strings exactly the same width."""
    return "".join(ch if ch in ".: " else "8" for ch in text)


def _seven_seg(draw, text, x, y, max_w, max_h, colour, anchor="mm", field=None) -> None:
    """Draw `text` as segment digits, scaled down to fit max_w x max_h.

    `field` is the display's *physical* set of cells, e.g. "18888" for a
    four-and-a-half digit readout. Given one, the value is drawn right-aligned
    within it at a fixed size and position, so a score gaining a digit
    mid-contest no longer resizes and reflows the whole panel -- which is both
    what a real instrument does and the only way the unlit backdrop can show
    the cells that aren't currently in use. A leading '1' is the half digit a
    real panel gives you for a leading 1 without paying for a full cell.

    Right-alignment is done by measuring the value rather than by padding it:
    DSEG7's space is only about a quarter of a cell wide, so a space-padded
    string does not line up with the field's own cells at all.

    Without a field, the all-lit form of the value serves as both backdrop and
    positioning reference -- a value containing '-' (the "--.-" placeholder)
    has a box only as tall as the middle segment, so anchoring on the value's
    own box would float the dashes above where the digits they replace sit."""
    if not text:
        return
    box = _all_segments(text)
    if field is not None and len(text) <= len(field):
        box = field
    # A leading "1" is a half digit: DSEG7 draws its two bars at the right of
    # the cell, so the left half is always blank. Charging a full cell of width
    # for it would shrink every other digit for nothing -- the visible extent
    # is half a cell narrower than the advance, and the string is drawn shifted
    # left by that much so the blank half falls outside the panel.
    half = box.startswith("1")
    size = max(6, round(max_h))
    while True:
        font = _dseg_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), box, font=font)
        w, h = right - left, bottom - top
        visible = w - (0.5 * draw.textlength("8", font=font) if half else 0.0)
        if size <= 6 or (visible <= max_w and h <= max_h):
            break
        size = max(6, int(size * 0.93))
    pad = 0.5 * draw.textlength("8", font=font) if half else 0.0
    vis = w - pad
    ax = x - vis / 2 - pad if anchor == "mm" else x - w if anchor == "rm" else x
    ay = y - h / 2 - top
    draw.text(
        (ax, ay), box, font=font, fill=tuple(round(c * HUD_SEG_DIM) for c in colour)
    )
    draw.text(
        (ax + w - draw.textlength(text, font=font), ay), text, font=font, fill=colour
    )


def _paste_needle(img: Image.Image, sprite, pivot, centre, az: float) -> None:
    """Paste a compass needle pointing at `az` degrees (0 = north, clockwise),
    turned about its own pivot -- the ball at its base, well below the middle
    of its bounding box, so rotating about the box centre would swing the whole
    needle around the compass instead of pointing it.

    Done by padding the sprite into a square canvas centred on that pivot,
    which turns "rotate about an arbitrary point" into PIL's own "rotate about
    the centre"."""
    px, py = pivot
    r = math.ceil(
        max(
            math.hypot(dx - px, dy - py)
            for dx in (0, sprite.width)
            for dy in (0, sprite.height)
        )
    )
    canvas = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    canvas.alpha_composite(sprite, (r - round(px), r - round(py)))
    turned = canvas.rotate(-az, resample=Image.BILINEAR)  # PIL turns the other way
    img.paste(turned, (round(centre[0]) - r, round(centre[1]) - r), turned)


# --- 5x7 dot-matrix font, for the CW ticker -------------------------------
#
# The ticker is a dot-matrix display, drawn dot by dot with the same lit/unlit
# treatment as the segment panels above. Written out as a table rather than
# taken from a font file: the glyph set is tiny and fully determined (MORSE
# can only ever decode to these 44 characters plus space), and at 5x7 a table
# is directly readable in the source -- each row of a glyph is visible as it
# will be drawn. Rendered as a sheet and eyeballed, since a mistyped row is a
# plausible-looking glyph rather than an error.
HUD_MATRIX_COLS, HUD_MATRIX_ROWS = 5, 7
# The display scrolls a whole dot column at a time, which is what a real
# dot-matrix panel does -- there are no sub-dot positions on one.
HUD_TICKER_CELL_COLS = HUD_MATRIX_COLS + 1
HUD_TICKER_COLS_PER_S = HUD_TICKER_CHARS * HUD_TICKER_CELL_COLS / HUD_TICKER_SPAN_S
_FONT_5X7 = {
    " ": "00000 00000 00000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    ",": "00000 00000 00000 00000 00110 00100 01000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 00110 00110",
    "/": "00001 00010 00010 00100 01000 01000 10000",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    "=": "00000 00000 11111 00000 11111 00000 00000",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
}


def _matrix_rows(ch: str) -> list[str]:
    return _FONT_5X7.get(ch, _FONT_5X7["?"]).split()


def _draw_matrix_text(draw, cells, rect, colour, width_chars) -> None:
    """Draw a `width_chars`-wide 5x7 dot-matrix display.

    Every dot is drawn -- lit ones in `colour`, the rest at HUD_SEG_DIM -- so
    an idle display still reads as a display. `cells` are (column offset,
    character) pairs from HudTimeline.at; offsets are whole dot columns, and a
    character partly past either edge is simply clipped there, which is how
    text scrolls onto and off a real panel."""
    x, y, w, h = rect
    cols = max(1, width_chars * HUD_TICKER_CELL_COLS - 1)
    # Integer pitch and dot size, not fractional: at fractional values PIL
    # rounds each rectangle independently, so gaps come out a pixel wide in
    # some columns and zero in others and the display stops reading as a grid.
    pitch = max(2, int(min(w / cols, h / HUD_MATRIX_ROWS)))
    dot = max(1, pitch - max(1, round(pitch * 0.18)))
    ox = x + (w - pitch * cols) // 2
    oy = y + (h - pitch * HUD_MATRIX_ROWS) // 2
    dim = tuple(round(c * HUD_SEG_DIM) for c in colour)

    def put(col: int, row: int, fill) -> None:
        if 0 <= col < cols:
            dx, dy = ox + col * pitch, oy + row * pitch
            draw.rectangle([dx, dy, dx + dot - 1, dy + dot - 1], fill=fill)

    for col in range(cols):
        for row in range(HUD_MATRIX_ROWS):
            put(col, row, dim)
    for offset, ch in cells:
        for row, bits in enumerate(_matrix_rows(ch)):
            for c, bit in enumerate(bits):
                if bit == "1":
                    put(offset + c, row, colour)


def _dim_region(img: Image.Image, rect, factor: float) -> None:
    """Darken one rectangle in place -- how an unselected band/mode chip and
    the unreached part of the S-meter are made. Both are baked (and pasted)
    *lit*, and dimmed back rather than being a separate unlit pair of assets,
    so nothing has to stay stylistically in sync."""
    x, y, w, h = rect
    box = (x, y, x + w, y + h)
    img.paste(img.crop(box).point(lambda v: round(v * factor)), box)


def _seg_in(draw, rect, text, colour, field=None, right=False) -> None:
    """Draw a segment readout into a recess, fitted to it. Right-aligned for
    the stats rows, whose values change width; centred everywhere else."""
    x, y, w, h = _inset(rect)
    if right:
        _seven_seg(draw, text, x + w, y + h // 2, w, h, colour, anchor="rm")
    else:
        _seven_seg(draw, text, x + w // 2, y + h // 2, w, h, colour, field=field)


def draw_hud_frame(state: HudState, art: HudArt) -> Image.Image:
    """Render one HUD bar: the artwork, plus this instant's values.

    Nothing static is drawn -- every label, frame and the compass rose come
    from the artwork itself (see HudArt), so this only ever paints readouts,
    sprites and dimming."""
    img = art.bar.copy()
    draw = ImageDraw.Draw(img)

    # --- SCORE (DOOM's health): the biggest number on the bar, flashing as
    # it counts up after each QSO.
    colour = tuple(
        round(c + (255 - c) * state.score_flash) for c in HUD_RED
    )  # washes toward white at the moment of a QSO
    _seg_in(draw, art.slots["score"], f"{state.score}", colour, field=HUD_SCORE_FIELD)
    _seg_in(draw, art.slots["qsos"], f"{state.qsos}", HUD_RED, field=HUD_QSOS_FIELD)

    # --- QRG, then dim whichever band/mode chips are not the current one.
    qrg = f"{state.freq_hz / 1e6:.3f}" if state.freq_hz else "---.---"
    _seg_in(draw, art.slots["freq"], qrg, HUD_AMBER, field=HUD_QRG_FIELD)
    for row, names, active in (
        ("band", _HUD_BANDS, state.band),
        ("mode", _HUD_MODES, state.mode),
    ):
        for rect, name in zip(art.chips[row], names):
            if name != active:
                _dim_region(img, rect, HUD_UNLIT_DIM)

    # --- RX/TX lamp. With no rig state at all the socket is simply left
    # empty, which is what the artwork already draws there.
    if state.ptt is not None:
        lamp = art.sprites["tx" if state.ptt else "rx"]
        img.paste(lamp, art.slots["lamp"][:2], lamp)

    # --- signal meter: the sprite is a fully lit LED bar, pasted over the
    # recess and then dimmed back from the current level rightwards, so lit and
    # unlit LEDs are the same artwork rather than two assets. Both the sprite
    # box and the slot hold the LED strip *itself*, with the frame around it
    # left to the artwork -- so the cut is simply lit/segments of the width,
    # and it lands in a gap rather than through an LED (a half-lit segment
    # reads as a rendering fault rather than as a reading). An earlier crop
    # took in the frame too, which then got dimmed along with the LEDs.
    x, y, w, h = art.slots["smeter"]
    img.paste(art.sprites["meter"], (x, y), art.sprites["meter"])
    lit = 0 if state.s_level is None else round(state.s_level * HUD_METER_SEGMENTS)
    if lit < HUD_METER_SEGMENTS:
        cut = x + round(w * lit / HUD_METER_SEGMENTS)
        _dim_region(img, (cut, y, x + w - cut, h), HUD_UNLIT_DIM)

    # --- compass: solid needle = where the rotator points, hollow needle =
    # bearing to the station being worked, so the swing onto target is visible.
    cx, cy, cw, ch = art.slots["compass"]
    centre = (cx + cw / 2, cy + ch / 2)
    # The hollow one goes on top: the two coinciding is the normal case, and
    # underneath the solid needle its outline would simply be invisible, so
    # "on target" would look identical to "no target known".
    for name, az in (("needle", state.rot_az), ("target", state.target_az)):
        if az is not None:
            _paste_needle(img, art.sprites[name], art.pivots[name], centre, az)

    # --- PWR: supply volts + PA current. No recording carries these yet --
    # the radio only reports them when polled, which the logger doesn't do --
    # so this renders placeholders rather than hiding, which would leave two
    # empty recesses on the bar looking like a fault.
    for name, value in (("vd", state.vd), ("id", state.id_a)):
        _seg_in(
            draw,
            art.slots[name],
            f"{value:.1f}" if value is not None else "--.-",
            HUD_RED,
        )

    # --- stats: values only, right-aligned. Their captions (UTC / RATE /H /
    # ODX KM) are part of the artwork, printed beside these recesses.
    for rect, value in zip(
        art.stats,
        (
            state.utc.strftime("%H:%M:%S") if state.utc else "--:--:--",
            f"{state.rate_per_h:.0f}",
            f"{state.best_km}",
        ),
    ):
        _seg_in(draw, rect, value, HUD_RED, right=True)

    # --- CW ticker: a fixed HUD_TICKER_CHARS-wide dot-matrix display, with
    # characters entering at the right edge as they are keyed. Uninset,
    # unlike every other readout: its slot is only seven dots tall to begin
    # with, so a margin there costs a whole dot of pitch. _draw_matrix_text
    # centres the grid in whatever it is given.
    _draw_matrix_text(
        draw, state.ticker, art.slots["ticker"], HUD_GREEN, HUD_TICKER_CHARS
    )
    return img


def hud_demo_state() -> HudState:
    """The mockup's own dummy values -- for --hud-demo, so the layout can be
    checked against the artwork with no recording at hand."""
    return HudState(
        t=0.0,
        utc=datetime(2026, 8, 3, 18, 42, 7),
        score=12847,
        qsos=63,
        rate_per_h=47,
        best_km=782,
        freq_hz=144_174_000,
        mode="CW",
        band="2M",
        ptt=False,
        rot_az=135,
        target_az=118,
        s_level=0.62,
        ticker=[(i * HUD_TICKER_CELL_COLS, c) for i, c in enumerate("TU 5NN JN86SR")],
        vd=13.8,
        id_a=12.4,
    )


HUD_H_FRAC = HUD_H / 1080  # bar height as a fraction of the frame, from the
# 1080p reference layout; scaled for other resolutions.


def hud_height(H: int) -> int:
    """The bar's pixel height for a given frame height, forced even.

    libx264 refuses an odd dimension, and 720p lands on 173 -- found only by
    rendering an actual clip at 720p, since the 1080p reference height is
    already even and every string-level test used it. One function rather than
    the same rounding in main() and render(), which must agree exactly or the
    bar is scaled to a different height than it was drawn at."""
    return 2 * round(H * HUD_H_FRAC / 2)


def hud_frame_key(state: HudState) -> tuple:
    """What the HUD's pixels actually depend on, for frame reuse.

    Everything except `t` itself, with the continuously-varying values
    quantised to the resolution they are *drawn* at: the meter is 18 discrete
    segments and a needle rounded to the nearest degree moves well under a
    pixel. Without this the scope-derived signal level alone would force a
    fresh draw ~30 times a second."""
    return (
        state.utc.replace(microsecond=0) if state.utc else None,
        state.score,
        round(state.score_flash, 2),
        state.qsos,
        round(state.rate_per_h),
        state.best_km,
        state.freq_hz,
        state.mode,
        state.band,
        state.ptt,
        None if state.rot_az is None else round(state.rot_az),
        None if state.target_az is None else round(state.target_az),
        None if state.s_level is None else round(state.s_level * 18),
        tuple(state.ticker),
        None if state.vd is None else round(state.vd, 1),
        None if state.id_a is None else round(state.id_a, 1),
    )


def render_hud_video(
    timeline: HudTimeline,
    out_path: str,
    art: HudArt,
    duration: float,
    fps: int = RENDER_FPS,
) -> int:
    """Render the HUD bar to its own clip, to be composited by render().

    Same separate-stage-then-composite pattern as render_cast_video and
    render_scope_video: PIL frames piped straight into ffmpeg as rawvideo,
    no intermediate PNGs. Its t=0 is the output timeline's t=0, so render()
    needs no -itsoffset for it at all -- unlike every other side stream here,
    this one is generated *from* that timeline rather than captured against
    an independent clock.

    Returns the number of frames actually drawn, which is what the reuse
    optimisation is measured by."""
    W, H = art.bar.size
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", f"{fps}",
        "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", out_path,
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    drawn = 0
    try:
        last_key = None
        frame = None
        for i in range(max(1, int(duration * fps))):
            state = timeline.at(i / fps)
            key = hud_frame_key(state)
            if key != last_key or frame is None:
                frame = draw_hud_frame(state, art).tobytes()
                last_key = key
                drawn += 1
            proc.stdin.write(frame)
    finally:
        proc.stdin.close()
        proc.wait()
    return drawn


def _stream_input_args(start: float, path: str) -> list[str]:
    """ffmpeg input args placing a side stream's frame 0 at `start`.

    A negative start (the stream began before the audio -- see stream_start)
    is an -ss seek *into* the stream, not a negative -itsoffset: ffmpeg has
    no meaningful "shift these timestamps earlier than the output starts",
    and the frames before t=0 are simply ones the output never shows."""
    if start < 0:
        return ["-ss", f"{-start:.3f}", "-i", path]
    return ["-itsoffset", f"{start:.3f}", "-i", path]


def render(
    wav: str,
    out: str,
    W: int,
    H: int,
    webcam: str | None = None,
    webcam_start: float = 0.0,
    webcam_rate: float = 0.0,
    cast: str | None = None,
    cast_start: float = 0.0,
    cast_rate: float = 0.0,
    scope: str | None = None,
    scope_start: float = 0.0,
    scope_end: float = 0.0,
    hud: str = "",
    hud_face: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-stats", "-loglevel", "warning", "-i", wav]

    # Inputs are added in this order when present: scope, cast, webcam --
    # indices computed up front so each branch below references its own
    # input by number regardless of which others are present, rather than
    # each branch guessing its own index from what came before it.
    # An input's index is taken at the moment it is appended rather than from
    # a separate list that has to be kept in the same order as the branches
    # below. Those two silently drifted apart the moment the HUD branch was
    # inserted ahead of the cast branch, and every stream then read another
    # stream's clip: the HUD was drawn at the cast PiP's position and size,
    # the terminal was squeezed into the webcam's face recess, and the webcam
    # was stretched full-width along the bottom where the HUD belongs. The
    # filter-graph string assertions all still passed, because each branch
    # was individually well-formed.
    def add_input(args: list[str]) -> int:
        idx = sum(1 for a in cmd if a == "-i")
        cmd.extend(args)
        return idx

    hud_h = hud_height(H)

    # Full-screen scrolling waterfall, dimmed to ~half luma so it reads as an
    # ambient background and the text stays crisp on top. overlap=0.8 makes it
    # scroll fast enough to fill the frame within the first few seconds.
    fchain = (
        f"[0:a]showspectrum=s={W}x{H}:mode=combined:slide=scroll:overlap=0.8:"
        f"color=intensity:scale=cbrt:fscale=log:saturation=1.6,"
        f"lutyuv=y=val*0.42,format=yuv420p,fps={RENDER_FPS}[specbg]"
    )
    bg = "specbg"
    if scope:
        # scope is our own render_scope_video output -- like the cast branch
        # (and unlike webcam), its own timestamps are real/absolute
        # (icom_net.py's write_scope_record uses real time.time() values),
        # so a plain -itsoffset positions it exactly, no drift-rate
        # correction needed. Drawn *under* the subtitles pass (unlike
        # cast/webcam, which sit on top of it as PiPs) so it acts as a real
        # replacement background rather than an inset -- the audio-derived
        # showspectrum layer stays underneath as a fallback for any stretch
        # the scope recording doesn't cover (didn't start recording yet,
        # stopped early, or a `--duration` cut lands outside its range).
        # tpad still guards against the shared filtergraph ending early the
        # same way it does for cast/webcam; enable='between(...)' (not
        # eof_action=pass) handles both the before-start and after-end gaps
        # with the one proven mechanism already used for those PiPs' own
        # start gate, rather than mixing two different techniques for the
        # same class of problem.
        scope_idx = add_input(_stream_input_args(scope_start, scope))
        fchain += (
            f";[{scope_idx}:v]scale={W}:{H},fps={RENDER_FPS},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=99999[scopebg]"
            f";[{bg}][scopebg]overlay=x=0:y=0:"
            f"enable='between(t,{max(scope_start, 0.0):.3f},{scope_end:.3f})'[bg2]"
        )
        bg = "bg2"
    cur = bg
    if cast:
        # cast is our own render_cast_video output -- a synthetic, constant-
        # framerate file we just encoded, so no drift *of its own* -- but
        # its internal timestamps came from asciinema's real-time capture
        # on the same laptop as the webcam, so the same laptop-clock-vs-
        # audio-clock drift measured via the webcam (see main(), cast_rate)
        # applies here too: setpts stretches its timeline the same way the
        # webcam branch's does, before the fps=RENDER_FPS resample. itsoffset
        # positions its own t=0 (the moment the logger started) at
        # cast_start in the output timeline. tpad clones its last frame so a
        # cast shorter than the round can't truncate the shared filtergraph,
        # same reasoning as the webcam branch below.
        # Height-constrained, not width-constrained: with a bar along the bottom
        # the terminal's limit is the room above it, and a width fraction
        # picked before the HUD existed simply overran it -- the logger's own
        # toolbar was drawn across the SCORE and QSOS panels.
        cast_x = round(W * CAST_PIP_X_FRAC)
        cast_y = round(H * CAST_PIP_MARGIN_FRAC)
        cast_scale = f"-2:{H - hud_h - 2 * cast_y}"
        cast_idx = add_input(_stream_input_args(cast_start, cast))
        # format=yuva420p + colorchannelmixer=aa lowers the PiP's alpha so the
        # overlay blends it over the waterfall (a little transparency, not a
        # wash) -- overlay honours the top input's own alpha channel.
        fchain += (
            f";[{cast_idx}:v]setpts=PTS/{1 - cast_rate:.8f},scale={cast_scale},fps={RENDER_FPS},"
            f"format=yuva420p,colorchannelmixer=aa={CAST_PIP_ALPHA},"
            f"tpad=stop_mode=clone:stop_duration=99999[castpip]"
            f";[{cur}][castpip]overlay=x={cast_x}:y={cast_y}:"
            f"enable='gte(t,{max(cast_start, 0.0):.3f})'[v1]"
        )
        cur = "v1"
    if hud:
        # No -itsoffset: unlike every other side stream here, the HUD clip is
        # generated *from* the output timeline rather than captured against an
        # independent clock, so its t=0 already is the output's t=0. Composited after
        # the cast (the bar is a status bar -- nothing overlaps it) and before
        # the webcam, which lands on top of it inside the face recess.
        hud_idx = add_input(["-i", hud])
        fchain += (
            f";[{hud_idx}:v]scale={W}:{hud_h},fps={RENDER_FPS},"
            f"tpad=stop_mode=clone:stop_duration=99999[hudbar]"
            f";[{cur}][hudbar]overlay=x=0:y=main_h-h[vhud]"
        )
        cur = "vhud"
    if webcam:
        # itsoffset delays the whole cam stream's presentation timestamps so
        # its own frame 0 lands at webcam_start in the output timeline --
        # exactly right, since that's the real moment the phone started
        # recording. tpad clones the cam's last frame indefinitely so a clip
        # a little shorter than the round (as here) can never end the
        # shared filtergraph early and truncate the main waterfall/audio.
        # The cam is *not* mirrored: the logger's own Alt+V capture records
        # the laptop webcam already the right way round (an earlier phone
        # front-camera capture recorded raw/un-mirrored and needed an hflip;
        # the same-machine capture that replaced it does not).
        #
        # fps=RENDER_FPS on this branch matters even though the source
        # already claims 30fps: a real phone recording verified against
        # this (ffprobe: r_frame_rate 30/1, but avg_frame_rate ~29.997,
        # derived from its actual per-frame timestamps) is genuinely
        # variable-rate under a constant-looking label -- not one big
        # pause but 3,444 scattered micro frame-drops across the ~2h
        # recording (checked directly via each packet's own pts_time;
        # typical of thermal/buffer pressure on a long phone capture),
        # summing to exactly 0.753s of extra real time the frame count
        # alone doesn't account for. Left unfiltered, this is a real
        # reported symptom (in sync at the start of the video, over a
        # second off by the end): the PiP was silently running very
        # slightly fast relative to the audio-driven main timeline the
        # whole way through, since something upstream of this filter
        # apparently laid its frames out by count rather than by their
        # own true timestamps. The fps filter resamples using the
        # decoder's true per-frame PTS as its reference, duplicating
        # frames onto a clean 30fps grid that absorbs every one of those
        # scattered drops and actually matches real elapsed time --
        # eliminating the drift instead of just reducing it.
        #
        # setpts=PTS/(1-webcam_rate), applied first (before fps resamples
        # onto a clean grid, so that resampling itself uses the corrected
        # timeline): the phone and the radio recorder are two independent
        # devices whose clocks don't tick at exactly the same *rate* --
        # see refine_webcam_start, which fits this rate from real audio
        # cross-correlation. A rate mismatch is a linear drift, which
        # -itsoffset (a constant shift) cannot correct on its own; scaling
        # every presentation timestamp by 1/(1-rate) stretches or
        # compresses the PiP's own timeline just enough to compensate,
        # while -itsoffset still handles the constant (intercept) part.
        # webcam_rate defaults to 0.0 (identity scaling) when no rate was
        # determined (e.g. --webcam-offset was used instead, or
        # cross-correlation found no confident match).
        # The webcam belongs in the artwork's own face recess, exactly where
        # DOOM's portrait sits. Cropped to the recess's aspect (a centre crop
        # of a webcam pointed at the operator *is* a face portrait) rather
        # than letterboxed, which would leave bars inside the frame.
        fx, fy, fw, fh = hud_face
        pip_x, pip_y = fx, H - hud_h + fy
        fit = f"crop=min(iw\\,ih*{fw}/{fh}):min(ih\\,iw*{fh}/{fw}),scale={fw}:{fh}"
        webcam_idx = add_input(["-itsoffset", f"{webcam_start:.3f}", "-i", webcam])
        fchain += (
            f";[{webcam_idx}:v]setpts=PTS/{1 - webcam_rate:.8f},fps={RENDER_FPS},"
            f"{fit},tpad=stop_mode=clone:stop_duration=99999[pip]"
            f";[{cur}][pip]overlay=x={pip_x}:y={pip_y}:"
            f"enable='gte(t,{webcam_start:.3f})'[v]"
        )
        cur = "v"
    if cur != "v":
        fchain += f";[{cur}]null[v]"
    cmd += [
        "-filter_complex",
        fchain,
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        out,
    ]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # recdir/edi are optional only so --hud-demo can run with no recording at
    # hand; every other mode still requires both (checked right after parsing).
    ap.add_argument("recdir", nargs="?", help="directory of timestamped WAV segments")
    ap.add_argument(
        "edi",
        nargs="*",
        help="EDI log(s) for the same round -- pass more than one "
        "to merge multiple bands worked in one recording",
    )
    ap.add_argument("-o", "--out", default="contest_video.mp4")
    ap.add_argument("--pitch", type=float, default=600.0, help="CW tone Hz")
    ap.add_argument("--res", choices=RESOLUTIONS, default="1080p")
    ap.add_argument(
        "--skip-gaps",
        action="store_true",
        help=f"trim silent gaps between QSOs to {GAP_KEEP_S:.0f}s each",
    )
    ap.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="keep the intermediate .wav and side-stream clips for inspection",
    )
    ap.add_argument(
        "--telemetry",
        help="puskas_logger *-telemetry.jsonl -- optional: the RX/TX + QRG/mode "
        "badge already comes from the WAV files' own IC-9700 metadata; this "
        "only adds bearing (ROT) and refines QRG/mode within long segments "
        "where the operator QSY'd with nothing to split the WAV on",
    )
    ap.add_argument(
        "--duration",
        type=float,
        help="trim to the first DURATION seconds of real round time "
        "(chronological preview; also skips CW-decoding past the "
        "cutoff, so a short preview is much faster to build)",
    )
    ap.add_argument(
        "--cast",
        help="asciinema cast (v2) recording of the logger/irssi terminal, "
        "shown as a large picture-in-picture -- synced from "
        "the cast header's own Unix-epoch timestamp, exact real-world "
        "UTC with no whole-hour rounding needed",
    )
    ap.add_argument(
        "--scope",
        help="icom_net.py *-scope recording (uv run icom_net.py <ip> --scope "
        "FILE) -- replaces the audio-derived showspectrum background with "
        "the radio's own real spectrum-scope sweeps wherever the recording "
        "covers, falling back to the audio waterfall elsewhere. Synced from "
        "each sweep's own Unix-epoch timestamp, exact like --cast",
    )
    ap.add_argument(
        "--webcam",
        help="picture-in-picture selfie/webcam clip, synced automatically "
        "from its own filename timestamp (e.g. VID_20260706_180003.mp4), "
        "then refined via audio cross-correlation against the operator's "
        "own TX audio (see --webcam-offset to override)",
    )
    ap.add_argument(
        "--webcam-offset",
        type=float,
        help="manual fine-tune correction (seconds, may be negative) added to "
        "the coarse whole-hour webcam sync -- bypasses the automatic audio "
        "cross-correlation entirely; use this if it finds no confident "
        "match (e.g. the webcam clip has no audio track), or to override it",
    )
    ap.add_argument(
        "--input-log",
        help="puskas_logger *-input.jsonl for exact QSO-panel/chapter/caption "
        "timing (its 'qso' events) instead of the EDI's minute-precision "
        "clock -- optional, older recordings won't have one",
    )
    ap.add_argument(
        "--hud-demo",
        metavar="OUT.png",
        help="write a single HUD bar filled with dummy values and exit -- no "
        "recording needed, for checking the layout against the artwork",
    )
    ap.add_argument(
        "--hud-preview",
        metavar="OUT.png",
        help="write a single HUD bar built from this recording's real data at "
        "--hud-preview-t and exit without rendering video (pair with "
        "--duration to keep the CW decode short)",
    )
    ap.add_argument(
        "--hud-preview-t",
        type=float,
        default=0.0,
        help="video-time position (seconds) sampled by --hud-preview",
    )
    ap.add_argument(
        "--hud-theme",
        metavar="DIR",
        default=HUD_THEME_DIR,
        help="HUD theme directory (artwork.png + theme.json), default the "
        "hud-theme/ next to this script",
    )
    ap.add_argument(
        "--hud-theme-check",
        nargs="?",
        const="",
        metavar="OUT.png",
        help="draw every rect in the theme's theme.json back onto its artwork "
        "and exit -- the way to check a hand-edited theme. With no path it "
        "writes <theme dir>/theme-check.png and opens it in the default image "
        "viewer; give a path to only write the file",
    )
    args = ap.parse_args()

    if args.hud_theme_check is not None:
        overlay = hud_theme_overlay(load_hud_theme(args.hud_theme))
        # Always write the file -- it is the scriptable, diffable artifact --
        # and additionally open a viewer when no path was asked for, since the
        # editing loop this exists for is GIMP on one side and a look on the
        # other, not a file to go hunting for.
        out = args.hud_theme_check or os.path.join(args.hud_theme, "theme-check.png")
        overlay.save(out)
        print(f"wrote {out} from {args.hud_theme}/theme.json")
        if not args.hud_theme_check:
            overlay.show()
        return

    if args.hud_demo:
        art = hud_art(load_hud_theme(args.hud_theme))
        draw_hud_frame(hud_demo_state(), art).save(args.hud_demo)
        print(f"wrote {args.hud_demo} (dummy values)")
        return

    if not args.recdir or not args.edi:
        ap.error("recdir and at least one EDI file are required")

    # Only the render is guarded: --hud-demo and --hud-theme-check write one
    # PNG and exit, and iterating the HUD's layout from the project root is
    # exactly how they are meant to be used.
    wiring.require_round_directory()

    W, H = RESOLUTIONS[args.res]
    segs = scan_segments(args.recdir)
    if not segs:
        sys.exit(f"no timestamped WAVs found in {args.recdir}")
    print(f"{len(segs)} segments, {segs[-1].audio_t + segs[-1].dur:.0f}s audio")

    my_callsign, mywwl, qsos_all = merge_edi(args.edi)
    offset_h = derive_utc_offset(segs, qsos_all)
    print(f"{my_callsign} {mywwl}: {len(qsos_all)} QSOs, UTC+{offset_h} local")

    cast_start = None
    cast_rate = 0.0
    if args.cast:
        cast_wall, cast_cols, cast_rows = parse_cast_header(args.cast)
        cast_start = stream_start(cast_wall + timedelta(hours=offset_h), segs)
        print(
            f"  cast: {cast_cols}x{cast_rows} terminal, synced to start at "
            f"{cast_start:.0f}s in the output (exact -- Unix-epoch timestamp; "
            f"see below for a clock-drift correction shared with --webcam, if given)"
        )

    scope_records: list[tuple[float, int, int, bytes]] = []
    scope_start = None
    scope_end = None
    if args.scope:
        scope_records = read_scope_records(args.scope)
        if len(scope_records) < 2:
            print(f"  scope: {args.scope} has fewer than 2 sweeps -- ignoring")
            scope_records = []
        else:
            first_wall = datetime.fromtimestamp(
                scope_records[0][0], tz=timezone.utc
            ).replace(tzinfo=None) + timedelta(hours=offset_h)
            last_wall = datetime.fromtimestamp(
                scope_records[-1][0], tz=timezone.utc
            ).replace(tzinfo=None) + timedelta(hours=offset_h)
            scope_start = stream_start(first_wall, segs)
            scope_end = audio_time_for(last_wall, segs)
            print(
                f"  scope: {len(scope_records)} sweeps, synced to "
                f"{scope_start:.0f}-{scope_end:.0f}s in the output "
                f"(exact -- Unix-epoch timestamps, same as --cast)"
            )

    webcam_start = None
    webcam_rate = 0.0
    webcam_exact = False
    if args.webcam:
        # Prefer, in order: the exact timestamp baked into the filename
        # itself (parse_webcam_precise_filename -- self-contained, no
        # sidecar file needed); then the ffmpeg log's frame-0 wallclock
        # (µs-precise, but depends on the *-webcam.log surviving alongside
        # the video); then the logger's webcam_start event (~1s early,
        # stamped before ffmpeg spawned). All three are same-machine, so
        # placement is exact either way, no cross-correlation needed.
        cam_wall = parse_webcam_precise_filename(args.webcam)
        src = "exact timestamp in filename"
        if cam_wall is None:
            log_path = os.path.splitext(args.webcam)[0] + ".log"
            cam_wall = (
                webcam_start_from_log(log_path) if os.path.exists(log_path) else None
            )
            src = "ffmpeg frame-0 wallclock"
        if cam_wall is None and args.input_log:
            cam_wall = webcam_start_wall(args.input_log)
            src = "logged webcam_start event"
        if cam_wall is not None:
            webcam_start = audio_time_for(cam_wall + timedelta(hours=offset_h), segs)
            webcam_exact = True
            print(
                f"  webcam: synced to start at {webcam_start:.0f}s in the output "
                f"(exact -- {src}, same-machine clock, no cross-correlation needed)"
            )
        else:
            cam_wall = parse_webcam_wall(args.webcam)
            cam_dur = _ffprobe_duration(args.webcam)
            webcam_start = sync_webcam_start(
                cam_wall, cam_dur, qsos_all, segs, offset_h
            )
            print(
                f"  webcam: synced to start at {webcam_start:.0f}s in the output (coarse, "
                f"whole-hour only -- see refine_webcam_start below)"
            )

    # read_wav_metadata runs before --duration trims segs (unlike the CW
    # decode loop further down, which *should* skip past the cutoff) so the
    # webcam fine-tune below can search for TX anchors across the *full*
    # round, same reasoning as sync_webcam_start using qsos_all above --
    # a short preview otherwise has too few candidates to find a confident
    # match.
    read_wav_metadata(segs)
    known_wav = sum(1 for s in segs if s.ptt is not None)
    print(f"  WAV metadata: {known_wav}/{len(segs)} segments have IC-9700 rig tags")

    if args.webcam and webcam_start is not None:
        if args.webcam_offset is not None:
            webcam_start += args.webcam_offset
            print(
                f"  webcam: manual offset {args.webcam_offset:+.2f}s applied -> "
                f"starts at {webcam_start:.2f}s (no drift-rate correction -- "
                f"pass no --webcam-offset to use automatic cross-correlation instead)"
            )
        else:
            # Even an exact filename/log-derived start only fixes the
            # constant offset -- the webcam capture (this machine's system
            # clock, via gettimeofday) and the radio recording (the WAV
            # sample clock, an independent crystal in the IC-9700) still
            # aren't ticking at exactly the same *rate*. Confirmed on a real
            # ~2h same-machine Alt+V recording: cross-correlation anchors
            # showed a consistent, low-noise linear drift (~-1.2s intercept,
            # residual std ~0.1s after outlier rejection) growing to ~+5s by
            # the end -- not measurement noise, and large enough to be
            # audible/visible. So refine_webcam_start always runs regardless
            # of webcam_exact; the exact start is still a much better seed
            # for the correlation search than the coarse whole-hour one.
            refined, rate, n = refine_webcam_start(args.webcam, segs, webcam_start)
            if n:
                intercept = refined - webcam_start
                print(
                    f"  webcam: audio cross-correlation refined start by "
                    f"{intercept:+.2f}s and found a "
                    f"{rate * 3600:+.3f}s/hour clock-drift rate using {n} anchor(s) "
                    f"-> starts at {refined:.2f}s"
                )
                webcam_start = refined
                webcam_rate = rate
                # The webcam capture and the cast recording (asciinema, also
                # on this machine) are timestamped by the *same* laptop
                # system clock -- so the same intercept/rate correction
                # measured against the webcam's own audio (the only stream
                # with anything to cross-correlate against the radio's WAV
                # audio) applies to the cast PiP too. Confirmed needed from
                # a real report: the operator saw the logger's own on-screen
                # mode change happen visibly before the audio caught up with
                # it, late in the same round this webcam drift was found
                # in -- consistent with one shared laptop-clock drift, not
                # two unrelated bugs.
                if args.cast and cast_start is not None:
                    cast_start += intercept
                    cast_rate = rate
                    print(
                        f"  cast: applying the same clock-drift correction "
                        f"({intercept:+.2f}s, {rate * 3600:+.3f}s/hour) -> "
                        f"starts at {cast_start:.2f}s"
                    )
            else:
                print(
                    "  webcam: audio cross-correlation found no confident match "
                    "(no audio track, or no TX segments long enough) -- using "
                    f"{'exact' if webcam_exact else 'coarse whole-hour'} sync only; "
                    "pass --webcam-offset to fine-tune manually"
                )

    if args.duration:
        segs = trim_to_duration(segs, args.duration)
        print(
            f"  duration: preview cut to first {args.duration:.0f}s "
            f"({len(segs)} segments)"
        )

    telemetry = load_telemetry(args.telemetry) if args.telemetry else []
    state_events = build_state_events(segs, telemetry, offset_h)
    known = sum(1 for _, _, st in state_events if st.ptt is not None)
    suffix = (
        f" ({args.telemetry} refines freq/mode within long segments)"
        if args.telemetry
        else ""
    )
    print(f"  RX/TX: {known} state changes{suffix}")

    print("decoding CW ...")
    # Segments longer than MAX_OVER_S are never decoded as a whole (see
    # decode_segment) -- but one can still contain a real CW exchange
    # between *other* stations that we only listened to, with no PTT of
    # our own to split the file on. decode_long_segment recovers those
    # from state_events' telemetry-confirmed CW sub-ranges. Offsets are
    # kept segment-relative (t0, t1) rather than resolved to absolute
    # video-timeline time here, so they stay valid even if remap_audio_t
    # (below, --skip-gaps) later shifts audio_t.
    long_cw_raw: list[tuple[Segment, float, float, list[CharEvent]]] = []
    for s in segs:
        if s.dur > MAX_OVER_S:
            for t0, t1, events in decode_long_segment(s, state_events, args.pitch):
                long_cw_raw.append((s, t0, t1, events))
            continue
        events, snr = decode_segment(s.path, args.pitch)
        s.events = gate_events(s.dur, events, snr)
    decoded = sum(len(s.events) for s in segs) + sum(
        len(ev) for _, _, _, ev in long_cw_raw
    )
    trusted_overs = sum(1 for s in segs if s.events) + len(long_cw_raw)
    print(f"  {decoded} characters from {trusted_overs} trusted overs")
    if long_cw_raw:
        print(
            f"  including {len(long_cw_raw)} CW exchange(s) recovered from "
            f"otherwise-too-long listening segments"
        )

    if args.skip_gaps:
        long_cw_segs = {id(s) for s, _, _, _ in long_cw_raw}
        remap_audio_t(segs, long_cw_segs)
        total = segs[-1].audio_t + _eff(segs[-1])
        print(
            f"  skip-gaps: {total:.0f}s video (was {segs[-1].audio_t + segs[-1].dur:.0f}s)"
        )

    total = segs[-1].audio_t + _eff(segs[-1])
    qsos = [
        q
        for q in qsos_all
        if audio_time_for(q.dt + timedelta(hours=offset_h), segs) < total
    ]
    if len(qsos) < len(qsos_all):
        print(f"  {len(qsos)}/{len(qsos_all)} QSOs fall within the {total:.0f}s cut")

    if webcam_start is not None and webcam_start >= total:
        print("  webcam starts after the cut ends -- dropping the PiP overlay")
        webcam_start = None

    if cast_start is not None and cast_start >= total:
        print("  cast starts after the cut ends -- dropping the PiP overlay")
        cast_start = None

    if scope_start is not None and scope_start >= total:
        print("  scope starts after the cut ends -- dropping the background")
        scope_records, scope_start, scope_end = [], None, None
    elif scope_end is not None:
        scope_end = min(scope_end, total)

    # Resolved to absolute video-timeline time only now, using each
    # segment's final audio_t (post-remap, if --skip-gaps was used).
    long_cw_spans = [
        (seg.audio_t + t0, seg.audio_t + t1, events)
        for seg, t0, t1, events in long_cw_raw
    ]

    # Only feeds qso_windows()'s exact chapter/caption timing now -- the
    # typewriter overlay this also used to drive is gone, since the
    # cast PIP already shows exactly what was typed, live.
    qso_times = None
    if args.input_log:
        input_log = load_input_log(args.input_log)
        qso_times = match_qso_times(qsos, input_log)
        matched = sum(1 for t in qso_times if t is not None)
        print(
            f"  {matched}/{len(qsos)} QSOs got an exact submit time from the input log"
        )

    stem = os.path.splitext(args.out)[0]
    windows = qso_windows(qsos, segs, offset_h, total, qso_times)

    if args.hud_preview:
        timeline = build_hud_timeline(
            segs,
            qsos,
            windows,
            mywwl,
            offset_h,
            state_events=state_events,
            scope_records=scope_records,
            long_cw_spans=long_cw_spans,
            telemetry=telemetry,
        )
        state = timeline.at(args.hud_preview_t)
        art = hud_art(load_hud_theme(args.hud_theme))
        draw_hud_frame(state, art).save(args.hud_preview)
        print(f"wrote {args.hud_preview} at t={args.hud_preview_t:.1f}s: {state}")
        return

    with open(stem + ".chapters.txt", "w") as fh:
        fh.write(build_chapters(qsos, windows))
    with open(stem + ".srt", "w") as fh:
        fh.write(build_srt(qsos, windows))
    print(f"wrote {stem}.chapters.txt and {stem}.srt")

    wav = os.path.splitext(args.out)[0] + ".concat.wav"
    print("concatenating audio ...")
    concat_audio(segs, wav)

    cast_video = None
    if args.cast and cast_start is not None:
        cast_video = stem + ".cast.mp4"
        # How much of the cast's own timeline the cut can ever display.
        # render() positions it with -itsoffset cast_start and stretches it by
        # cast_rate, so clip time tau shows at cast_start + tau/(1-cast_rate);
        # invert that at tau = total. The margin keeps tpad's frame-cloning
        # from being visible at the very end of a preview.
        cast_span = (total - cast_start) * (1 - cast_rate) + STREAM_TRIM_MARGIN_S
        print("rendering cast PiP ...")
        render_cast_video(args.cast, cast_video, max_duration=cast_span)

    hud_video = stem + ".hud.mp4"
    hud_art_ = hud_art(load_hud_theme(args.hud_theme), W, hud_height(H))
    print("rendering HUD ...")
    drawn = render_hud_video(
        build_hud_timeline(
            segs,
            qsos,
            windows,
            mywwl,
            offset_h,
            state_events=state_events,
            scope_records=scope_records,
            long_cw_spans=long_cw_spans,
            telemetry=telemetry,
        ),
        hud_video,
        hud_art_,
        total,
    )
    frames = max(1, int(total * RENDER_FPS))
    print(f"  {drawn} frames drawn for {frames} ({frames / max(1, drawn):.0f}x reuse)")

    scope_video = None
    if scope_records and scope_start is not None:
        scope_video = stem + ".scope.mp4"
        # The overlay is gated to scope_end, so anything past it is invisible.
        scope_span = min(total, scope_end or total) - scope_start
        print("rendering scope waterfall background ...")
        render_scope_video(
            args.scope,
            scope_video,
            W,
            H,
            max_duration=scope_span + STREAM_TRIM_MARGIN_S,
        )

    print("rendering (this takes a while) ...")
    render(
        wav,
        args.out,
        W,
        H,
        webcam=args.webcam if webcam_start is not None else None,
        webcam_start=webcam_start or 0.0,
        webcam_rate=webcam_rate,
        cast=cast_video,
        cast_start=cast_start or 0.0,
        cast_rate=cast_rate,
        scope=scope_video,
        scope_start=scope_start or 0.0,
        scope_end=scope_end or 0.0,
        hud=hud_video,
        hud_face=hud_art_.slots["face"],
    )

    if not args.keep_intermediates:
        os.remove(wav)
        if cast_video:
            os.remove(cast_video)
        if scope_video:
            os.remove(scope_video)
        os.remove(hud_video)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
