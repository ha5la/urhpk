# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Produce an annotated CW contest video from a recording + EDI log.

Given a directory of timestamped WAV segments (split on RX/TX switches, as
recorded during the contest) and the EDI log for the same round, this builds a
YouTube-ready MP4 with:

  * a scrolling audio spectrogram (SDR-style waterfall) as background
  * a live CW decode ticker, synced to the audio
  * a panel showing the current QSO from the log

Everything is emitted as one ASS subtitle file and burned in a single ffmpeg
pass -- no frame-by-frame rendering.

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
import json
import os
import re
import statistics
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

# ---------------------------------------------------------------------------
# CW decoding
# ---------------------------------------------------------------------------

MORSE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E', '..-.': 'F',
    '--.': 'G', '....': 'H', '..': 'I', '.---': 'J', '-.-': 'K', '.-..': 'L',
    '--': 'M', '-.': 'N', '---': 'O', '.--.': 'P', '--.-': 'Q', '.-.': 'R',
    '...': 'S', '-': 'T', '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X',
    '-.--': 'Y', '--..': 'Z', '-----': '0', '.----': '1', '..---': '2',
    '...--': '3', '....-': '4', '.....': '5', '-....': '6', '--...': '7',
    '---..': '8', '----.': '9', '.-.-.-': '.', '--..--': ',', '..--..': '?',
    '-..-.': '/', '-...-': '=', '.-.-.': '+', '-....-': '-', '-.-.--': '!',
}

ENV_FS = 200        # envelope sample rate (Hz) after demodulation
LOWPASS_CUTOFF_HZ = 120.0  # envelope filter cutoff -- covers real CW keying bandwidth
LOWPASS_NTAPS = 321        # windowed-sinc length; longer than the old boxcar for a
                           # much sharper stopband (rejects moderate-offset QRM
                           # noticeably better -- verified against real recordings)
THR_HI_FRAC = 0.5   # hysteresis: fraction of (peak-floor) to trigger "on"
THR_LO_FRAC = 0.3   # hysteresis: fraction of (peak-floor) to release back to "off"

# A segment's decode is trusted (shown in the ticker) only if it looks like a
# real over rather than band noise. The long "listening / calling CQ" stretches
# between QSOs carry many overlapping signals and noise at the CW pitch, which a
# single-tone decoder turns into gibberish; these three gates reject them while
# keeping every genuine exchange.
MAX_OVER_S = 30.0    # a real over is short; long segments are listening periods
MIN_SNR_DB = 20.0    # reject weak noise-only segments
MIN_QUALITY = 0.5    # reject text dominated by isolated single letters (noise)
MAX_DOMINANCE = 0.4  # reject text where one letter dominates (chopped carrier)


def _quality(text: str) -> float:
    """Fraction of whitespace tokens longer than one char. Noise decodes to a
    stream of single letters (E/T/I/S); real overs to callsigns and reports."""
    toks = [t for t in text.split(' ') if t]
    if not toks:
        return 0.0
    return 1.0 - sum(1 for t in toks if len(t) == 1) / len(toks)


def _dominance(text: str) -> float:
    """Share of the most common non-space character. A chopped steady carrier
    decodes to a run of one letter (TTTTT / EEEEE); real text is diverse."""
    chars = [c for c in text if c != ' ']
    if not chars:
        return 1.0
    return max(chars.count(c) for c in set(chars)) / len(chars)


def gate_events(dur: float, events: list["CharEvent"], snr: float) -> list["CharEvent"]:
    """Return events if the segment is a trustworthy over, else []."""
    text = ''.join(e.ch for e in events)
    if (dur < MAX_OVER_S and snr >= MIN_SNR_DB
            and _quality(text) >= MIN_QUALITY
            and _dominance(text) <= MAX_DOMINANCE):
        return events
    return []


@dataclass
class CharEvent:
    t: float   # seconds, relative to segment start
    ch: str


def _lowpass_kernel(cutoff_hz: float, sr: int, ntaps: int) -> np.ndarray:
    """Windowed-sinc lowpass FIR, unit DC gain. Much sharper stopband than a
    boxcar of the same length, so moderate-offset interference (roughly
    150 Hz+ away) is rejected noticeably better; interference much closer
    than that overlaps the wanted signal's own keying spectrum and can't be
    separated by filtering alone, at any filter shape."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff_hz / sr * n) * np.hanning(ntaps)
    return h / h.sum()


def _envelope(x: np.ndarray, sr: int, pitch: float) -> tuple[np.ndarray, float]:
    """Complex-demodulate at `pitch` and return the low-rate magnitude envelope."""
    t = np.arange(len(x)) / sr
    iq = x * np.exp(-2j * np.pi * pitch * t)
    h = _lowpass_kernel(LOWPASS_CUTOFF_HZ, sr, LOWPASS_NTAPS)
    env = np.abs(np.convolve(iq, h, 'same'))
    win = max(1, int(sr / ENV_FS))
    return env[::win], sr / win


def _hysteresis_on(env: np.ndarray, thr_hi: float, thr_lo: float) -> np.ndarray:
    """Schmitt-trigger on/off detection: a single static threshold lets noise
    sitting near it chatter on/off and corrupt run timing. Two thresholds with
    a margin between them need a real swing to change state."""
    on = np.empty(len(env), dtype=bool)
    state = False
    for i, v in enumerate(env):
        if state:
            state = v >= thr_lo
        else:
            state = v > thr_hi
        on[i] = state
    return on


def decode_segment(path: str, pitch: float = 600.0) -> tuple[list[CharEvent], float]:
    """Decode one WAV segment into timed characters and its SNR in dB.

    Returns (events, snr_db). Events is empty when the segment carries no
    keyed CW (flat envelope / silence)."""
    w = wave.open(path)
    sr = w.getframerate()
    n_frames = w.getnframes()
    if n_frames / sr > MAX_OVER_S:
        # gate_events rejects any segment this long regardless of decode
        # quality -- skip the expensive filtering/thresholding pipeline over
        # what can be several minutes of "listening" audio.
        w.close()
        return [], 0.0
    x = np.frombuffer(w.readframes(n_frames), dtype=np.int16).astype(float)
    w.close()
    if len(x) < sr * 0.5:
        return [], 0.0

    env, efs = _envelope(x, sr, pitch)
    floor = np.percentile(env, 25)
    peak = np.percentile(env, 95)
    snr = 20.0 * float(np.log10((peak + 1) / (floor + 1)))
    if peak < floor * 1.6:
        # flat envelope -> steady tone / noise, not keyed CW: skip
        return [], snr
    thr_hi = floor + THR_HI_FRAC * (peak - floor)
    thr_lo = floor + THR_LO_FRAC * (peak - floor)
    on = _hysteresis_on(env, thr_hi, thr_lo)

    # run-length encode on/off
    runs: list[tuple[bool, float, int]] = []  # (is_on, duration_s, start_idx)
    i = 0
    while i < len(on):
        j = i
        while j < len(on) and on[j] == on[i]:
            j += 1
        runs.append((bool(on[i]), (j - i) / efs, i))
        i = j

    ons = [d for s, d, _ in runs if s]
    if len(ons) < 3:
        return [], snr
    # dit = median of the shorter (dit) cluster of ON durations. Split dits from
    # dahs at the midpoint between the robust min/max so the estimate holds even
    # when an over is dah-heavy (a plain median lands between dit and dah and
    # collapses the two).
    lo = float(np.percentile(ons, 10))
    hi = float(np.percentile(ons, 90))
    dits = [d for d in ons if d <= (lo + hi) / 2] or ons
    dit = float(np.median(dits))
    if dit <= 0:
        return [], snr

    events: list[CharEvent] = []
    sym = ''
    sym_start = 0.0
    for s, d, idx in runs:
        t0 = idx / efs
        u = d / dit
        if s:
            if not sym:
                sym_start = t0
            sym += '.' if u < 2.0 else '-'
        else:
            if u >= 2.0 and sym:          # end of character
                ch = MORSE.get(sym, '')
                if ch:
                    events.append(CharEvent(sym_start, ch))
                sym = ''
            if u >= 5.0:                  # word gap
                if events and events[-1].ch != ' ':
                    events.append(CharEvent(t0, ' '))
    if sym:
        ch = MORSE.get(sym, '')
        if ch:
            events.append(CharEvent(sym_start, ch))
    return events, snr


# ---------------------------------------------------------------------------
# Timeline + EDI
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    path: str
    wall: datetime      # local wall-clock start (from filename)
    dur: float          # seconds (full recorded duration)
    audio_t: float      # start offset in the output video (seconds)
    events: list[CharEvent] = field(default_factory=list)
    eff_dur: float | None = None  # trimmed duration in output; None = use full dur


def _eff(s: Segment) -> float:
    return s.dur if s.eff_dur is None else s.eff_dur


@dataclass
class Qso:
    dt: datetime        # UTC (from EDI)
    call: str
    rst_s: str
    nr_s: str
    rst_r: str
    nr_r: str
    loc: str
    pts: int
    dup: bool


def scan_segments(recdir: str) -> list[Segment]:
    segs: list[Segment] = []
    audio_t = 0.0
    files = sorted(f for f in os.listdir(recdir) if f.lower().endswith('.wav'))
    for f in files:
        try:
            wall = datetime.strptime(f[:15], '%Y%m%d_%H%M%S')
        except ValueError:
            continue
        p = os.path.join(recdir, f)
        w = wave.open(p)
        dur = w.getnframes() / w.getframerate()
        w.close()
        segs.append(Segment(p, wall, dur, audio_t))
        audio_t += dur
    return segs


GAP_KEEP_S = 3.0  # seconds kept from each silent gap when --skip-gaps is used


def remap_audio_t(segs: list[Segment]) -> None:
    """Shorten gap segments to GAP_KEEP_S and recompute audio_t for all segments.

    A gap segment is one with no trusted decoded events and a duration longer
    than MAX_OVER_S — i.e. a listening / calling-CQ stretch between QSOs.
    Call this *after* gate_events has been applied to s.events.
    """
    t = 0.0
    for s in segs:
        s.audio_t = t
        if not s.events and s.dur > MAX_OVER_S:
            s.eff_dur = GAP_KEEP_S
        t += _eff(s)


def trim_to_duration(segs: list[Segment], max_dur: float) -> list[Segment]:
    """Keep only the segments needed to cover the first max_dur seconds of
    real session time (a --duration preview), shortening the last one to
    land exactly on the cutoff.

    Called *before* CW decoding, not after: decode_segment/gate_events are
    the expensive part of the pipeline, and a short preview has no use for
    segments past the cutoff, so this skips decoding them at all rather than
    decoding the full session and discarding most of the result.
    """
    out = [s for s in segs if s.audio_t < max_dur]
    if out:
        last = out[-1]
        cut = max(0.0, min(_eff(last), max_dur - last.audio_t))
        if cut < _eff(last):
            last.eff_dur = cut
    return out


def parse_edi(path: str) -> tuple[str, str, list[Qso]]:
    mycall, mywwl = '', ''
    qsos: list[Qso] = []
    in_records = False
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.rstrip('\n')
        if line.startswith('PCall='):
            mycall = line.split('=', 1)[1].strip()
        elif line.startswith('PWWLo='):
            mywwl = line.split('=', 1)[1].strip()
        elif line.startswith('[QSORecords'):
            in_records = True
            continue
        elif line.startswith('['):
            in_records = False
        elif in_records and line:
            f = line.split(';')
            if len(f) < 11:
                continue
            dt = datetime.strptime(f[0] + f[1], '%y%m%d%H%M')
            try:
                pts = int(f[10]) if f[10] else 0
            except ValueError:
                pts = 0
            dup = len(f) > 13 and f[13].strip().upper() == 'D'
            qsos.append(Qso(dt, f[2], f[4], f[5], f[6], f[7], f[9], pts, dup))
    return mycall, mywwl, qsos


def merge_edi(paths: list[str]) -> tuple[str, str, list[Qso]]:
    """Merge one or more per-band EDI logs (e.g. 2M + 70CM from the same
    session) into a single chronological QSO list -- the recording is one
    continuous audio timeline regardless of how many bands were worked."""
    mycall, mywwl = '', ''
    qsos: list[Qso] = []
    for path in paths:
        mc, mw, qs = parse_edi(path)
        if not mycall:
            mycall, mywwl = mc, mw
        qsos.extend(qs)
    qsos.sort(key=lambda q: q.dt)
    return mycall, mywwl, qsos


def audio_time_for(wall: datetime, segs: list[Segment]) -> float:
    """Map a local wall-clock time to a position in the output video."""
    for s in segs:
        if wall < s.wall:
            return s.audio_t
        if wall < s.wall + timedelta(seconds=s.dur):
            offset = min((wall - s.wall).total_seconds(), _eff(s))
            return s.audio_t + offset
    return segs[-1].audio_t + _eff(segs[-1])


def _utc_at(t: float, segs: list[Segment], offset_h: int) -> datetime | None:
    """Return the UTC time corresponding to video position `t`."""
    for s in segs:
        if s.audio_t <= t < s.audio_t + _eff(s):
            local = s.wall + timedelta(seconds=(t - s.audio_t))
            return local - timedelta(hours=offset_h)
    return None


def derive_utc_offset(segs: list[Segment], qsos: list[Qso]) -> int:
    """Integer-hour offset such that qso_utc + offset ~= wav local time."""
    if not qsos:
        return 0
    wav_mid = segs[0].wall + timedelta(
        seconds=(segs[-1].audio_t + segs[-1].dur) / 2)
    qso_mid = qsos[0].dt + (qsos[-1].dt - qsos[0].dt) / 2
    return round((wav_mid - qso_mid).total_seconds() / 3600)


_WEBCAM_TS_RE = re.compile(r'(\d{8}_\d{6})')


def parse_webcam_wall(path: str) -> datetime:
    """Parse a phone/webcam filename's embedded timestamp (e.g.
    VID_20260706_180003.mp4) the same way scan_segments reads WAV filenames."""
    m = _WEBCAM_TS_RE.search(os.path.basename(path))
    if not m:
        raise ValueError(f"no YYYYMMDD_HHMMSS timestamp found in {path}")
    return datetime.strptime(m.group(1), '%Y%m%d_%H%M%S')


def sync_webcam_start(cam_wall: datetime, cam_dur: float, qsos: list[Qso],
                      segs: list[Segment], offset_h: int) -> float:
    """Video-timeline position (seconds) where the webcam recording begins.

    The webcam is a separate device with its own clock convention, which
    need not match the WAV recorder's (in practice the WAV recorder here
    stamped filenames in plain UTC, while the phone stamped its own in local
    wall time -- two different offsets for the same session). So its offset
    can't be assumed to equal `offset_h`; it's derived the same way
    `offset_h` itself was, by treating the whole webcam clip as a one-segment
    "recording" and reusing derive_utc_offset's span-midpoint match against
    the *full* QSO list (not any --duration-trimmed subset, since a short
    preview's QSO span is too narrow an anchor for reliable hour rounding).
    """
    cam_seg = Segment('', cam_wall, cam_dur, 0.0)
    cam_offset_h = derive_utc_offset([cam_seg], qsos)
    cam_utc_start = cam_wall - timedelta(hours=cam_offset_h)
    return audio_time_for(cam_utc_start + timedelta(hours=offset_h), segs)


# ---------------------------------------------------------------------------
# Rig/rotator state -- ground truth from puskas_logger's 1 Hz telemetry, but
# displayed on the WAV segment boundaries, since those are split exactly on
# the real PTT transitions and so carry far better time precision than a
# once-a-second poll.
# ---------------------------------------------------------------------------

@dataclass
class TelemetrySample:
    t: datetime
    ptt: bool | None
    freq_hz: int | None
    mode: str | None
    az: float | None


@dataclass
class SegState:
    ptt: bool | None = None
    freq_hz: int | None = None
    mode: str | None = None
    az: float | None = None


def load_telemetry(path: str) -> list[TelemetrySample]:
    """Parse a puskas_logger `*-telemetry.jsonl` file."""
    samples: list[TelemetrySample] = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec['t'], '%Y-%m-%dT%H:%M:%SZ')
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        samples.append(TelemetrySample(ts, rec.get('ptt'), rec.get('freq_hz'),
                                       rec.get('mode'), rec.get('az')))
    return samples


def _majority(values: list):
    """Most common value (ties broken by first-seen order); None if empty."""
    if not values:
        return None
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda v: counts[v])


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def align_telemetry_to_segments(segs: list[Segment], telemetry: list[TelemetrySample],
                                offset_h: int) -> list[SegState]:
    """One state per WAV segment -- ptt/freq_hz/mode by majority vote (they
    rarely change mid-over), az by median -- from the 1 Hz samples whose
    timestamp falls inside that segment's wall-clock span; falls back to the
    nearest sample in time if none fall inside (a segment can be shorter than
    the 1 s telemetry interval). The segment's own boundary *times* are used
    for display, not the telemetry timestamps -- see module docstring above."""
    states: list[SegState] = []
    for s in segs:
        utc_start = s.wall - timedelta(hours=offset_h)
        utc_end = utc_start + timedelta(seconds=s.dur)
        inside = [t for t in telemetry if utc_start <= t.t < utc_end]
        if not inside and telemetry:
            inside = [min(telemetry, key=lambda t: abs((t.t - utc_start).total_seconds()))]
        states.append(SegState(
            ptt=_majority([t.ptt for t in inside if t.ptt is not None]),
            freq_hz=_majority([t.freq_hz for t in inside if t.freq_hz is not None]),
            mode=_majority([t.mode for t in inside if t.mode is not None]),
            az=_median([t.az for t in inside if t.az is not None]),
        ))
    return states


# ---------------------------------------------------------------------------
# ASS generation
# ---------------------------------------------------------------------------

RESOLUTIONS = {'1080p': (1920, 1080), '720p': (1280, 720)}
VIS_CHARS = 84          # characters kept in the decode ticker window
CPL = 42                # characters per ticker line
TICKER_HOLD_S = 3.0     # ticker clears if no new character arrives within this long


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _wrap(text: str, cpl: int, keep: int) -> str:
    lines: list[str] = []
    cur = ''
    for tok in text.split(' '):
        piece = tok if not cur else cur + ' ' + tok
        if len(piece) > cpl and cur:
            lines.append(cur)
            cur = tok
        else:
            cur = piece
    if cur:
        lines.append(cur)
    return '\\N'.join(lines[-keep:])


def _esc(s: str) -> str:
    return s.replace('\\', '\\\\').replace('{', '(').replace('}', ')')


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


def qso_windows(qsos: list[Qso], segs: list[Segment], offset_h: int,
                total: float) -> list[tuple[float, float]]:
    """Return the (start, end) video-time window shown for each QSO's panel,
    snapped onto the actual WAV segment/burst boundaries (see cluster_starts)
    rather than the EDI log's minute-precision timestamp, so the panel
    switches exactly when the real over begins."""
    clusters = cluster_starts(segs)
    starts = [_snap_to_cluster(audio_time_for(q.dt + timedelta(hours=offset_h), segs), clusters)
             for q in qsos]
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])   # keep panel order sane
    windows: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        windows.append((max(0.0, start), max(start + 1.0, end)))
    return windows


STATE_TX_HEX = '0000FF'  # ASS \c is &HbbggrrH -- this is pure red
STATE_RX_HEX = '00FF00'  # pure green


def _fmt_rig_info(freq_hz: int | None, mode: str | None, az: float | None) -> str | None:
    """QRG/mode/bearing line under the RX/TX badge; None if nothing is known."""
    if freq_hz is None and mode is None and az is None:
        return None
    parts = []
    if freq_hz is not None:
        parts.append(f"{freq_hz / 1e6:.3f} MHz")
    if mode is not None:
        parts.append(mode)
    parts.append(f"ROT {az:.0f}°" if az is not None else "ROT ---")
    return "  ".join(parts)


def build_ass(segs: list[Segment], qsos: list[Qso], mycall: str, mywwl: str,
              contest: str, offset_h: int, W: int, H: int,
              seg_states: list[SegState] | None = None) -> str:
    sx = W / 1920  # scale factor from the 1080p reference layout
    fs_big = int(58 * sx)
    fs_panel = int(46 * sx)
    fs_hdr = int(40 * sx)
    fs_clk = int(34 * sx)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Ticker,DejaVu Sans Mono,{fs_big},&H00FFFF66,&H000000FF,&H00000000,&H8C100C08,-1,0,0,0,100,100,0,0,3,10,0,5,60,60,0,1
Style: Panel,DejaVu Sans Mono,{fs_panel},&H00FFFFFF,&H000000FF,&H00000000,&HC8202018,-1,0,0,0,100,100,0,0,3,6,0,1,60,60,80,1
Style: PanelDup,DejaVu Sans Mono,{fs_panel},&H007070FF,&H000000FF,&H00000000,&HC8101040,-1,0,0,0,100,100,0,0,3,6,0,1,60,60,80,1
Style: Header,DejaVu Sans Mono,{fs_hdr},&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,8,60,60,40,1
Style: Clock,DejaVu Sans Mono,{fs_clk},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,4,0,9,60,60,40,1
Style: State,DejaVu Sans Mono,{fs_hdr},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,7,60,60,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = [head]

    def ev(start, end, style, text, layer=0):
        lines.append(
            f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},"
            f"{style},,0,0,0,,{text}")

    total = segs[-1].audio_t + _eff(segs[-1]) if segs else 0.0

    # --- static header (callsign / contest) ---
    ev(0, total, 'Header', f"{_esc(mycall)}  {_esc(mywwl)}   {_esc(contest)}")

    # --- rig/rotator state (top-left): one event per WAV segment, since
    # segment boundaries are split exactly on the real PTT transitions --
    # far better time precision than the 1 Hz telemetry the state itself
    # comes from. No event at all when ptt is unknown for that segment.
    if seg_states is not None:
        for s, st in zip(segs, seg_states):
            if st.ptt is None:
                continue
            hexcol = STATE_TX_HEX if st.ptt else STATE_RX_HEX
            label = 'TX' if st.ptt else 'RX'
            text = f"{{\\c&H{hexcol}&}}● {label}"
            info = _fmt_rig_info(st.freq_hz, st.mode, st.az)
            if info:
                text += f"\\N{{\\c&HFFFFFF&}}{_esc(info)}"
            ev(s.audio_t, s.audio_t + _eff(s), 'State', text)

    # --- decode ticker: rolling window, flushed at the start of every fresh
    # burst of on-air activity (see cluster_starts) -- not at a QSO's EDI
    # timestamp, which is only minute-precision and would flush mid-over.
    stream: list[tuple[float, str, bool]] = []   # (t, ch, flush_before)
    prev_was_gap = True
    for s in segs:
        if not s.events:
            prev_was_gap = s.dur > MAX_OVER_S
            continue
        is_burst_start = prev_was_gap
        if not is_burst_start and stream:
            stream.append((s.audio_t, ' ', False))   # gap between overs, same burst
        for j, e in enumerate(s.events):
            stream.append((s.audio_t + e.t, e.ch, is_burst_start and j == 0))
        prev_was_gap = False
    transcript = ''
    for i, (t, ch, flush) in enumerate(stream):
        if flush:
            transcript = ''
        transcript += ch
        vis = transcript[-VIS_CHARS:]
        end = stream[i + 1][0] if i + 1 < len(stream) else total
        end = min(end, t + TICKER_HOLD_S)   # clear rather than show stale text in gaps
        if end <= t:
            continue
        ev(t, end, 'Ticker', _wrap(vis, CPL, 2))

    # --- UTC clock: one event per second, top-right corner ---
    for sec in range(int(total) + 1):
        utc = _utc_at(float(sec), segs, offset_h)
        if utc:
            ev(float(sec), float(sec) + 1.0, 'Clock',
               utc.strftime('%Y-%m-%d %H:%M:%SZ'))

    # --- QSO panels ---
    windows = qso_windows(qsos, segs, offset_h, total)
    for i, q in enumerate(qsos):
        start, end = windows[i]
        tag = '  \\N{\\c&H7070FF&}*** DUPE (0 pts) ***' if q.dup else ''
        style = 'PanelDup' if q.dup else 'Panel'
        txt = (f"QSO {i + 1}/{len(qsos)}   {q.dt.strftime('%H:%MZ')}"
               f"\\N{_esc(q.call)}   {_esc(q.loc)}   {q.pts} km"
               f"\\NTX {_esc(q.rst_s)} {_esc(q.nr_s)}"
               f"    RX {_esc(q.rst_r)} {_esc(q.nr_r)}{tag}")
        ev(start, end, style, txt, layer=1)

    return ''.join(x if x.endswith('\n') else x + '\n' for x in lines)


# ---------------------------------------------------------------------------
# YouTube chapters + SRT captions (for seeking without scrubbing)
# ---------------------------------------------------------------------------

MIN_CHAPTER_GAP_S = 10   # YouTube ignores chapters closer together than this
CAPTION_DUR_S = 8.0      # how long each SRT cue is shown


def _yt_time(t: float) -> str:
    """Format seconds as a YouTube description timestamp (M:SS or H:MM:SS)."""
    t = int(round(max(0.0, t)))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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
        tag = " (dup)" if q.dup else ""
        lines.append(f"{_yt_time(t)} QSO {i + 1:03d} {q.call}{tag}")
        last_t = t
    return '\n'.join(lines) + '\n'


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
        tag = "  *** DUPE ***" if q.dup else ""
        text = (f"QSO {i + 1}/{len(qsos)}  {q.dt.strftime('%H:%MZ')}\n"
                f"{q.call}  {q.loc}  {q.pts} km\n"
                f"TX {q.rst_s} {q.nr_s}   RX {q.rst_r} {q.nr_r}{tag}")
        blocks.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return '\n'.join(blocks)


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def concat_audio(segs: list[Segment], out_wav: str) -> None:
    listfile = out_wav + '.txt'
    with open(listfile, 'w') as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s.path)}'\n")
            if s.eff_dur is not None:
                fh.write(f"outpoint {s.eff_dur:.6f}\n")
    subprocess.run(
        ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'concat', '-safe', '0', '-i', listfile, '-c', 'copy', out_wav],
        check=True)
    os.remove(listfile)


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', path],
        check=True, capture_output=True, text=True).stdout
    return float(out.strip())


PIP_WIDTH_FRAC = 0.20   # webcam PiP width as a fraction of the frame width
PIP_MARGIN_FRAC = 0.02  # gap from the frame edge, same fraction basis


def render(wav: str, ass: str, out: str, W: int, H: int,
          webcam: str | None = None, webcam_start: float = 0.0) -> None:
    ass_esc = ass.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
    # Full-screen scrolling waterfall, dimmed to ~half luma so it reads as an
    # ambient background and the text stays crisp on top. overlap=0.8 makes it
    # scroll fast enough to fill the frame within the first few seconds.
    fchain = (
        f"[0:a]showspectrum=s={W}x{H}:mode=combined:slide=scroll:overlap=0.8:"
        f"color=intensity:scale=cbrt:fscale=log:saturation=1.6,"
        f"lutyuv=y=val*0.42,format=yuv420p,fps=30[bg];"
        f"[bg]subtitles='{ass_esc}':fontsdir=/usr/share/fonts[v0]"
    )
    cmd = ['ffmpeg', '-y', '-hide_banner', '-stats', '-loglevel', 'warning',
           '-i', wav]
    if webcam:
        # itsoffset delays the whole cam stream's presentation timestamps so
        # its own frame 0 lands at webcam_start in the output timeline --
        # exactly right, since that's the real moment the phone started
        # recording. tpad clones the cam's last frame indefinitely so a clip
        # a little shorter than the session (as here) can never end the
        # shared filtergraph early and truncate the main waterfall/audio.
        # hflip un-mirrors a phone front-camera recording, which records raw
        # (not mirrored like the on-screen viewfinder the operator saw).
        pip_w = round(W * PIP_WIDTH_FRAC)
        margin = round(W * PIP_MARGIN_FRAC)
        cmd += ['-itsoffset', f'{webcam_start:.3f}', '-i', webcam]
        fchain += (
            f";[1:v]scale={pip_w}:-2,hflip,tpad=stop_mode=clone:stop_duration=99999[pip]"
            f";[v0][pip]overlay=x=main_w-w-{margin}:y=main_h-h-{margin}:"
            f"enable='gte(t,{webcam_start:.3f})'[v]"
        )
    else:
        fchain = fchain.replace('[v0]', '[v]')
    cmd += ['-filter_complex', fchain,
           '-map', '[v]', '-map', '0:a',
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '21',
           '-c:a', 'aac', '-b:a', '96k', '-shortest', out]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('recdir', help='directory of timestamped WAV segments')
    ap.add_argument('edi', help='EDI log for the same round')
    ap.add_argument('-o', '--out', default='contest_video.mp4')
    ap.add_argument('--pitch', type=float, default=600.0, help='CW tone Hz')
    ap.add_argument('--res', choices=RESOLUTIONS, default='1080p')
    ap.add_argument('--contest', default='URH OB 2026 - CW')
    ap.add_argument('--skip-gaps', action='store_true',
                    help=f'trim silent gaps between QSOs to {GAP_KEEP_S:.0f}s each')
    ap.add_argument('--keep-ass', action='store_true',
                    help='keep intermediate .ass/.wav for inspection')
    ap.add_argument('--telemetry',
                    help='puskas_logger *-telemetry.jsonl for an RX/TX + QRG/mode/bearing overlay')
    args = ap.parse_args()

    W, H = RESOLUTIONS[args.res]
    segs = scan_segments(args.recdir)
    if not segs:
        sys.exit(f"no timestamped WAVs found in {args.recdir}")
    print(f"{len(segs)} segments, {segs[-1].audio_t + segs[-1].dur:.0f}s audio")

    print("decoding CW ...")
    for s in segs:
        events, snr = decode_segment(s.path, args.pitch)
        s.events = gate_events(s.dur, events, snr)
    decoded = sum(len(s.events) for s in segs)
    print(f"  {decoded} characters from "
          f"{sum(1 for s in segs if s.events)} trusted overs")

    if args.skip_gaps:
        remap_audio_t(segs)
        total = segs[-1].audio_t + _eff(segs[-1])
        print(f"  skip-gaps: {total:.0f}s video (was {segs[-1].audio_t + segs[-1].dur:.0f}s)")

    mycall, mywwl, qsos = parse_edi(args.edi)
    offset_h = derive_utc_offset(segs, qsos)
    print(f"{mycall} {mywwl}: {len(qsos)} QSOs, UTC+{offset_h} local")

    seg_states = None
    if args.telemetry:
        telemetry = load_telemetry(args.telemetry)
        seg_states = align_telemetry_to_segments(segs, telemetry, offset_h)
        known = sum(1 for st in seg_states if st.ptt is not None)
        print(f"  RX/TX: {known}/{len(segs)} segments labelled from {args.telemetry}")

    ass_text = build_ass(segs, qsos, mycall, mywwl, args.contest,
                         offset_h, W, H, seg_states)
    ass_path = os.path.splitext(args.out)[0] + '.ass'
    with open(ass_path, 'w') as fh:
        fh.write(ass_text)

    stem = os.path.splitext(args.out)[0]
    total = segs[-1].audio_t + _eff(segs[-1])
    windows = qso_windows(qsos, segs, offset_h, total)
    with open(stem + '.chapters.txt', 'w') as fh:
        fh.write(build_chapters(qsos, windows))
    with open(stem + '.srt', 'w') as fh:
        fh.write(build_srt(qsos, windows))
    print(f"wrote {stem}.chapters.txt and {stem}.srt")

    wav = os.path.splitext(args.out)[0] + '.concat.wav'
    print("concatenating audio ...")
    concat_audio(segs, wav)
    print("rendering (this takes a while) ...")
    render(wav, ass_path, args.out, W, H)

    if not args.keep_ass:
        os.remove(wav)
        os.remove(ass_path)
    print(f"wrote {args.out}")


if __name__ == '__main__':
    main()
