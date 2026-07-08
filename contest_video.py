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
THR_HI_FRAC = 0.35  # hysteresis: fraction of (peak-floor) to trigger "on"
THR_LO_FRAC = 0.15  # hysteresis: fraction of (peak-floor) to release back to "off"
DEBOUNCE_DIT_FRAC = 0.5  # on/off runs shorter than this fraction of the segment's
                         # own preliminary dit estimate are noise, not real keying
                         # -- merged into their neighbour (see _debounce_on)

# A segment's decode is trusted (shown in the ticker) only if it looks like a
# real over rather than band noise. The long "listening / calling CQ" stretches
# between QSOs carry many overlapping signals and noise at the CW pitch, which a
# single-tone decoder turns into gibberish; these three gates reject them while
# keeping every genuine exchange.
MAX_OVER_S = 35.0    # a real over is short; long segments are listening periods.
                     # No clean statistical gap here (unlike e.g.
                     # FREQ_MATCH_TOLERANCE_HZ) -- real segment durations form
                     # a continuum from 30s up past 100s, so this is a modest,
                     # evidence-backed nudge (was 30.0) to capture one confirmed
                     # real 32.5s exchange with a full locator exchange, not a
                     # broad guess. The other three gates (SNR/quality/dominance)
                     # still guard against genuine long listening periods that
                     # happen to fall in the 30-35s range.
MIN_SNR_DB = 20.0    # reject weak noise-only segments
MIN_QUALITY = 0.5    # reject text dominated by isolated single letters (noise)
MAX_DOMINANCE = 0.4  # reject text where one letter dominates (chopped carrier)
MIN_CHARS_FOR_DOMINANCE = 5  # below this length, dominance is structurally
                             # high regardless of content -- see _dominance


def _quality(text: str) -> float:
    """Fraction of whitespace tokens longer than one char. Noise decodes to a
    stream of single letters (E/T/I/S); real overs to callsigns and reports."""
    toks = [t for t in text.split(' ') if t]
    if not toks:
        return 0.0
    return 1.0 - sum(1 for t in toks if len(t) == 1) / len(toks)


def _dominance(text: str) -> float:
    """Share of the most common non-space character. A chopped steady carrier
    decodes to a run of one letter (TTTTT / EEEEE); real text is diverse.

    Exempts short text (< MIN_CHARS_FOR_DOMINANCE) from this check
    entirely: a 2-character decode has dominance >= 0.5 by construction
    (either both characters match, or -- the *only* other option -- they
    don't, giving exactly 1/2) regardless of content, which made
    MAX_DOMINANCE=0.4 structurally impossible to pass for any two-letter
    contest word ("TU", "R", "K"...). Found from a real reported case:
    correctly-decoded "TU" and "73EE" were being silently dropped from
    the ticker. The "chopped carrier" pattern this guards against only
    shows up over many characters in practice anyway (see test_contest_video)."""
    chars = [c for c in text if c != ' ']
    if len(chars) < MIN_CHARS_FOR_DOMINANCE:
        return 0.0
    return max(chars.count(c) for c in set(chars)) / len(chars)


def gate_events(dur: float, events: list["CharEvent"], snr: float,
                check_duration: bool = True) -> list["CharEvent"]:
    """Return events if the segment is a trustworthy over, else [].

    check_duration=False skips the MAX_OVER_S check -- for telemetry-
    confirmed CW sub-ranges extracted from an otherwise-too-long segment
    (see decode_long_segment), where the duration gate's usual purpose --
    rejecting a segment whose unexplained length makes it suspicious --
    doesn't apply: telemetry mode confirmation is already stronger evidence
    than length that this specific span is genuine CW, not noise."""
    text = ''.join(e.ch for e in events)
    if ((not check_duration or dur < MAX_OVER_S) and snr >= MIN_SNR_DB
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


PITCH_SEARCH_LO_HZ = 300.0
PITCH_SEARCH_HI_HZ = 1600.0


def _detect_pitch(x: np.ndarray, sr: int, fallback: float) -> float:
    """Find the actual dominant tone frequency in a segment, rather than
    trusting a single assumed pitch for the whole session.

    A received signal's true beat note can be very different from the
    operator's own TX sidetone -- confirmed against real data far more
    dramatically than the ~70 Hz WAV/telemetry-frequency disagreement
    found earlier: one real RX segment's true tone was ~1296 Hz against
    the assumed 600 Hz, a 695 Hz gap entirely outside the envelope
    lowpass's passband (LOWPASS_CUTOFF_HZ=120), so almost none of the
    actual signal survived demodulation at the wrong frequency at all --
    not a decode-quality problem but a near-total loss of the signal
    before decoding even started. TX segments' own sidetone is reliably
    the loudest peak in the search band regardless (verified: several real
    TX segments across two different QSOs all auto-detected to within
    ~1 Hz of the nominal 600 Hz), so always detecting is safe rather than
    only doing it conditionally."""
    if len(x) < 8:
        return fallback
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    mask = (freqs >= PITCH_SEARCH_LO_HZ) & (freqs <= PITCH_SEARCH_HI_HZ)
    if not mask.any() or not spec[mask].any():
        return fallback
    return float(freqs[mask][np.argmax(spec[mask])])


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


def _debounce_on(on: np.ndarray, min_samples: int) -> np.ndarray:
    """Absorb on/off runs shorter than min_samples into the preceding run.

    A received (not the operator's own TX sidetone) signal is weaker and
    noisier, and the hysteresis thresholds -- however well tuned -- still
    let brief spikes/dropouts near the threshold flip state for a handful
    of samples. Verified against a real received-CW segment with known
    ground truth text: those brief flips fragmented single dits/dahs into
    several shorter pieces, corrupting the decode into gibberish despite a
    high overall SNR (33 dB) -- SNR measures the signal's average
    loudness, not the cleanliness of individual element edges. Left
    unfiltered, decode was unusable; with this debounce it recovered the
    great majority of the actual text."""
    if min_samples <= 1:
        return on
    out = on.copy()
    i = 0
    n = len(out)
    while i < n:
        j = i
        while j < n and out[j] == out[i]:
            j += 1
        if j - i < min_samples and i > 0:
            out[i:j] = out[i - 1]
        i = j
    return out


def _run_length_encode(on: np.ndarray, efs: float) -> list[tuple[bool, float, int]]:
    """(is_on, duration_s, start_sample_idx) for each run in `on`."""
    runs: list[tuple[bool, float, int]] = []
    i = 0
    n = len(on)
    while i < n:
        j = i
        while j < n and on[j] == on[i]:
            j += 1
        runs.append((bool(on[i]), (j - i) / efs, i))
        i = j
    return runs


def _estimate_dit(runs: list[tuple[bool, float, int]]) -> float | None:
    """Median of the shorter (dit) cluster of ON durations, or None if
    there aren't enough ON runs to estimate from. Split dits from dahs at
    the midpoint between the robust min/max so the estimate holds even
    when an over is dah-heavy (a plain median lands between dit and dah
    and collapses the two)."""
    ons = [d for s, d, _ in runs if s]
    if len(ons) < 3:
        return None
    lo = float(np.percentile(ons, 10))
    hi = float(np.percentile(ons, 90))
    dits = [d for d in ons if d <= (lo + hi) / 2] or ons
    dit = float(np.median(dits))
    return dit if dit > 0 else None


def _decode_samples(x: np.ndarray, sr: int, pitch: float = 600.0) -> tuple[list[CharEvent], float]:
    """Decode a raw sample buffer into timed characters and its SNR in dB --
    the actual demod/hysteresis/debounce/decode pipeline, factored out of
    decode_segment so decode_long_segment (see below) can run the same
    pipeline on an extracted sub-range of a WAV file instead of always the
    whole thing.

    `pitch` is only a fallback for the rare case _detect_pitch can't find
    anything (e.g. a silent segment) -- the actual demodulation frequency
    is always auto-detected, see _detect_pitch's docstring for why a single
    assumed pitch for the whole session doesn't hold.

    Returns (events, snr_db). Events is empty when the signal carries no
    keyed CW (flat envelope / silence)."""
    if len(x) < sr * 0.5:
        return [], 0.0

    pitch = _detect_pitch(x, sr, pitch)
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

    # Debounce, but relative to a *preliminary* dit estimate, not a fixed
    # time: a fixed threshold that's short enough to only catch noise at
    # slow WPM is longer than a real dit at high WPM and starts eating
    # legitimate fast keying (confirmed: a fixed 30ms threshold silently
    # dropped all decode at 45 WPM, where a dit is ~27ms). DEBOUNCE_DIT_FRAC
    # of the *segment's own* preliminary dit estimate scales correctly
    # with whatever speed this particular over turns out to be.
    prelim_dit = _estimate_dit(_run_length_encode(on, efs))
    if prelim_dit:
        on = _debounce_on(on, max(1, int(efs * DEBOUNCE_DIT_FRAC * prelim_dit)))

    runs = _run_length_encode(on, efs)
    dit = _estimate_dit(runs)
    if dit is None:
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


def decode_segment(path: str, pitch: float = 600.0) -> tuple[list[CharEvent], float]:
    """Decode one whole WAV segment into timed characters and its SNR in dB.

    Returns (events, snr_db). Events is empty when the segment carries no
    keyed CW (flat envelope / silence)."""
    w = wave.open(path)
    sr = w.getframerate()
    n_frames = w.getnframes()
    if n_frames / sr > MAX_OVER_S:
        # gate_events rejects any segment this long regardless of decode
        # quality -- skip the expensive filtering/thresholding pipeline over
        # what can be several minutes of "listening" audio. The one
        # exception is a telemetry-confirmed CW sub-range *within* such a
        # segment, which decode_long_segment (below) handles separately by
        # extracting and decoding just that sub-range.
        w.close()
        return [], 0.0
    x = np.frombuffer(w.readframes(n_frames), dtype=np.int16).astype(float)
    w.close()
    return _decode_samples(x, sr, pitch)


def _read_wav_range(path: str, t0: float, t1: float) -> tuple[np.ndarray, int]:
    """Read samples in [t0, t1) seconds from a WAV file without loading the
    whole file -- for extracting one sub-range out of a long segment (see
    decode_long_segment). t0/t1 are clamped to the file's own bounds."""
    w = wave.open(path)
    sr = w.getframerate()
    n_frames = w.getnframes()
    f0 = max(0, min(n_frames, int(t0 * sr)))
    f1 = max(f0, min(n_frames, int(t1 * sr)))
    w.setpos(f0)
    x = np.frombuffer(w.readframes(f1 - f0), dtype=np.int16).astype(float)
    w.close()
    return x, sr


def cw_subranges(seg: "Segment", state_events: list[tuple[float, float, "SegState"]]
                 ) -> list[tuple[float, float]]:
    """Telemetry-confirmed CW-mode time ranges within `seg`'s own span,
    expressed as (start, end) offsets in seconds relative to the segment's
    own start (0..seg.dur) -- deliberately not absolute video-timeline
    seconds, so the result stays valid even if audio_t is later remapped
    (see decode_long_segment and remap_audio_t's long_cw_segs parameter).

    Only meaningful for a segment too long to decode as a whole (see
    decode_long_segment): our own recorder only splits a new WAV file on
    our own PTT, so a segment where we just listened to someone else's
    entire exchange -- possibly spanning several of their own mode changes
    -- stays one long file. state_events (from build_state_events) already
    carries the right sub-division for this, seeded from the WAV's own
    starting mode and refined by telemetry wherever it shows a genuine
    change within the segment."""
    seg_start, seg_end = seg.audio_t, seg.audio_t + seg.dur
    out: list[tuple[float, float]] = []
    for start, end, st in state_events:
        if st.mode != 'CW':
            continue
        s0, s1 = max(start, seg_start), min(end, seg_end)
        if s1 > s0:
            out.append((s0 - seg.audio_t, s1 - seg.audio_t))
    return out


def decode_long_segment(seg: "Segment", state_events: list[tuple[float, float, "SegState"]],
                        pitch: float = 600.0) -> list[tuple[float, float, list[CharEvent]]]:
    """Recover CW content from a segment too long to decode as a whole (see
    MAX_OVER_S) by decoding just its telemetry-confirmed CW-mode sub-ranges,
    if any -- e.g. two other stations negotiating a CW frequency over voice,
    working each other in CW, then moving on, all while we just listened
    without ever keying up ourselves, so our recorder never split the file.

    Each returned (t0, t1, events) is relative to the segment's own start,
    like cw_subranges -- resolve to absolute video-timeline time (seg.audio_t
    + t0) only once the final audio_t is known, i.e. after any --skip-gaps
    remap. Each CharEvent's own .t is relative to that sub-range's start
    (t0), not the segment's.

    The sub-range's own duration is deliberately *not* checked against
    MAX_OVER_S (gate_events(..., check_duration=False)): a real two-way
    exchange between other stations can easily run longer than one of our
    own overs, and the duration gate's only purpose is rejecting segments
    whose unexplained length makes them suspicious -- telemetry mode
    confirmation is already stronger evidence than length that this
    specific span is genuine CW, not noise. SNR/quality/dominance still
    apply.

    One known limitation: the two stations may key at noticeably different
    speeds, but dit-length is estimated once across the whole sub-range
    (see _estimate_dit), which can degrade accuracy for whichever side
    differs most from that single estimate."""
    out: list[tuple[float, float, list[CharEvent]]] = []
    for t0, t1 in cw_subranges(seg, state_events):
        x, sr = _read_wav_range(seg.path, t0, t1)
        events, snr = _decode_samples(x, sr, pitch)
        events = gate_events(t1 - t0, events, snr, check_duration=False)
        if events:
            out.append((t0, t1, events))
    return out


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
    freq_hz: int | None = None    # from the WAV's own IC-9700 metadata (read_wav_metadata)
    mode: str | None = None       # ditto
    ptt: bool | None = None       # ditto -- ground truth at the segment's own start, no telemetry lag


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


_WAV_TITLE_RE = re.compile(
    r'(\d+)\.(\d+)\.(\d+)\s+(\S+)\s+.*?(RX|TX)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*$')
_SSB_ALIASES = ('USB', 'LSB', 'AM', 'DSB', 'SAM')  # matches puskas_logger.py's _mode_str


def parse_wav_title(title: str) -> tuple[int, str, bool] | None:
    """Parse an IC-9700 'Voice Recorder' title tag, e.g.
    'IC-9700 Voice Recorder Data   144.299.84 USB    ----.---.-- ------ -- '
    'TX 2026-07-06 16:00:37' -> (144299840, 'SSB', True).

    This is ground truth straight from the radio at the exact instant it
    started recording the file -- unlike telemetry (a separate 1 Hz poll,
    not synced to the WAV split at all), there is no possible lag here.
    Returns None if the title doesn't match this format (not an IC-9700
    recording, or a future firmware changing it)."""
    m = _WAV_TITLE_RE.search(title)
    if not m:
        return None
    mhz, khz, h10, mode, rxtx = m.groups()
    freq_hz = int(mhz) * 1_000_000 + int(khz) * 1_000 + int(h10) * 10
    if mode in _SSB_ALIASES:
        mode = 'SSB'
    return freq_hz, mode, rxtx == 'TX'


def _read_wav_title(path: str) -> str | None:
    """Read the LIST/INFO/INAM ('title') tag directly from a WAV file's own
    RIFF chunk structure -- no subprocess. ffprobe can read the same tag
    but spawning it once per file doesn't scale: measured 707 files at
    ~112s via ffprobe vs. ~0.02s reading the raw chunk headers directly."""
    with open(path, 'rb') as f:
        header = f.read(12)
        if len(header) < 12 or header[0:4] != b'RIFF' or header[8:12] != b'WAVE':
            return None
        while True:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                return None
            chunk_id = chunk_header[0:4]
            chunk_size = int.from_bytes(chunk_header[4:8], 'little')
            if chunk_id == b'LIST':
                data = f.read(chunk_size)
                if chunk_size % 2:
                    f.read(1)  # chunks are padded to an even size
                if data[0:4] == b'INFO':
                    pos = 4
                    while pos + 8 <= len(data):
                        sub_id = data[pos:pos + 4]
                        sub_size = int.from_bytes(data[pos + 4:pos + 8], 'little')
                        sub_data = data[pos + 8:pos + 8 + sub_size]
                        if sub_id == b'INAM':
                            return sub_data.rstrip(b'\x00').decode('ascii', errors='replace')
                        pos += 8 + sub_size + (sub_size % 2)
            else:
                f.seek(chunk_size + (chunk_size % 2), 1)


def read_wav_metadata(segs: list[Segment]) -> None:
    """Populate freq_hz/mode/ptt on each segment straight from its own WAV
    file's embedded IC-9700 metadata. Leaves them None for a file with no
    recognized tag -- no fallback heuristic, since there's nothing to
    fall back to that's as trustworthy (see build_state_events)."""
    for s in segs:
        title = _read_wav_title(s.path)
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
        samples.append(TelemetrySample(ts, rec.get('freq_hz'),
                                       rec.get('mode'), rec.get('az')))
    return samples


@dataclass
class InputLogEvent:
    t: datetime
    kind: str            # 'text' (keystroke) or 'qso' (an actual submit)
    text: str = ''        # kind == 'text': the full input-box contents
    call: str = ''        # kind == 'qso'
    dup: bool = False     # kind == 'qso'


def load_input_log(path: str) -> list[InputLogEvent]:
    """Parse a puskas_logger `*-input.jsonl` log. Two event kinds share the
    file (see puskas_logger.py's own comment on why): 'text' is one line per
    keystroke feeding the typewriter overlay, microsecond-precise but with
    no reliable way to tell a submit from an abort. 'qso' is one line per
    QSO actually appended to the log, written from the one place that
    unambiguously knows -- see match_qso_times, which uses it to give QSO
    panels an exact submit time instead of the EDI's minute-precision guess."""
    out: list[InputLogEvent] = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec['t'], '%Y-%m-%dT%H:%M:%S.%fZ')
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        kind = rec.get('event', 'text')
        out.append(InputLogEvent(ts, kind, rec.get('text', ''),
                                 rec.get('call', ''), rec.get('dup', False)))
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


FREQ_MATCH_TOLERANCE_HZ = 500  # see build_state_events' docstring


def build_state_events(segs: list[Segment], telemetry: list[TelemetrySample],
                       offset_h: int) -> list[tuple[float, float, SegState]]:
    """RX/TX + QRG/mode/bearing badge events.

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
    later 1 Hz sample shows them actually changing -- seeded from the WAV's
    starting value, not from telemetry, so a segment with no telemetry
    change at all just keeps the WAV-sourced value for its whole span.

    az has no equivalent in the WAV metadata at all and is purely
    telemetry's own -- the median of whichever samples make up each
    freq/mode run.

    Comparing the two frequency sources exactly (Hz for Hz) is unsound:
    the WAV metadata and rigctld-via-telemetry don't agree to the exact
    Hz even when nothing changed. Checked against this real session's own
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
        inside = sorted((t for t in telemetry if utc_start <= t.t < utc_end),
                        key=lambda t: t.t)

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
            freq_changed = (new_freq is not None and cur_freq is not None
                            and abs(new_freq - cur_freq) > FREQ_MATCH_TOLERANCE_HZ)
            mode_changed = new_mode is not None and new_mode != cur_mode
            if freq_changed or mode_changed:
                cur_freq, cur_mode = new_freq, new_mode
                runs.append(((cur_freq, cur_mode), [t]))
            else:
                runs[-1][1].append(t)

        seg_end = s.audio_t + _eff(s)
        for i, (key, samples) in enumerate(runs):
            start = s.audio_t if i == 0 else \
                audio_time_for(samples[0].t + timedelta(hours=offset_h), segs)
            end = (audio_time_for(runs[i + 1][1][0].t + timedelta(hours=offset_h), segs)
                   if i + 1 < len(runs) else seg_end)
            if end <= start:
                continue
            freq_hz, mode = key
            events.append((start, end, SegState(
                ptt=s.ptt, freq_hz=freq_hz, mode=mode,
                az=_median([t.az for t in samples if t.az is not None]),
            )))
    return events


def build_input_events(input_log: list[InputLogEvent], segs: list[Segment],
                       offset_h: int, total: float) -> list[tuple[float, float, str]]:
    """(start, end, text) windows for the typewriter overlay -- one per
    recorded input-box state, shown verbatim from the moment it was typed
    until the next keystroke changes it. Only 'text' events matter here (a
    'qso' event doesn't change what's on screen). Unlike the CW ticker or
    the QSO panels, this needs no burst-snapping heuristic at all: every
    record's timestamp is the operator's own keystroke, already exact
    ground truth. Empty-text windows (buffer cleared by Enter/Escape) are
    dropped rather than rendered, so nothing shows while the input line is
    genuinely idle."""
    keystrokes = [e for e in input_log if e.kind == 'text']
    if not keystrokes:
        return []
    times = [audio_time_for(e.t + timedelta(hours=offset_h), segs) for e in keystrokes]
    windows: list[tuple[float, float, str]] = []
    for i, e in enumerate(keystrokes):
        if not e.text:
            continue
        start = times[i]
        end = times[i + 1] if i + 1 < len(times) else total
        if end <= start:
            continue
        windows.append((start, end, e.text))
    return windows


def match_qso_times(qsos: list[Qso], input_log: list[InputLogEvent]) -> list[datetime | None]:
    """Precise submit timestamp for each qsos[i], from the input log's 'qso'
    events -- an exact replacement for the EDI's minute-precision q.dt when
    available, None otherwise (older recordings, or a --duration cut that
    excludes the matching event).

    Matched by call, in chronological order *within that call* -- not by
    exact time. Time-based matching was tried first (q.dt is exactly the
    minute-truncation of the same real moment an automatically-generated
    'qso' event's microsecond timestamp records, since puskas_logger derives
    both from one captured `now`) but rejected: it silently breaks for a
    hand-crafted log seeded from the EDI and then hand-tuned against the
    audio (see --seed-input-log) the moment an edited timestamp crosses a
    minute boundary from what the EDI happened to record -- exactly the kind
    of edit this feature exists to make possible. Call+order has no such
    trap: a --duration cut only ever removes a *suffix* in time, so the
    surviving occurrences of any call are still a prefix of the full
    sequence, and "next unused" stays correct."""
    by_call: dict[str, list[datetime]] = {}
    for e in input_log:
        if e.kind == 'qso':
            by_call.setdefault(e.call, []).append(e.t)
    used: dict[str, int] = {}
    out: list[datetime | None] = []
    for q in qsos:
        i = used.get(q.call, 0)
        cands = by_call.get(q.call, [])
        if i < len(cands):
            out.append(cands[i])
            used[q.call] = i + 1
        else:
            out.append(None)
    return out


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


def qso_windows(qsos: list[Qso], segs: list[Segment], offset_h: int, total: float,
                qso_times: list[datetime | None] | None = None) -> list[tuple[float, float]]:
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
        if precise is not None and snapped == prev_cluster and finishes[i - 1] is not None:
            starts.append(finishes[i - 1])
        else:
            starts.append(snapped)
        finishes.append(anchor_t if precise is not None else None)
        prev_cluster = snapped
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])   # keep panel order sane
    windows: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        fallback_end = starts[i + 1] if i + 1 < len(starts) else total
        end = finishes[i] if finishes[i] is not None else fallback_end
        windows.append((max(0.0, start), max(start + 1.0, end)))
    return windows


def running_score(qsos: list[Qso]) -> list[tuple[int, int]]:
    """(qso_count, cumulative_points) after each qsos[i]. Matches
    puskas_logger's own scoring convention (see its _band_summary and the
    EDI's CQSOP): every QSO counts toward qso_count, including dups -- they
    still get logged and shown, just worth nothing -- but only non-dup QSOs
    contribute points."""
    count = 0
    pts = 0
    out: list[tuple[int, int]] = []
    for q in qsos:
        count += 1
        if not q.dup:
            pts += q.pts
        out.append((count, pts))
    return out


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


def _mode_at(t: float, state_events: list[tuple[float, float, SegState]]) -> str | None:
    for start, end, st in state_events:
        if start <= t < end:
            return st.mode
    return None


def build_ass(segs: list[Segment], qsos: list[Qso], mycall: str, mywwl: str,
              contest: str, offset_h: int, W: int, H: int,
              state_events: list[tuple[float, float, SegState]] | None = None,
              input_events: list[tuple[float, float, str]] | None = None,
              qso_times: list[datetime | None] | None = None,
              long_cw_spans: list[tuple[float, float, list[CharEvent]]] | None = None) -> str:
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
Style: Input,DejaVu Sans Mono,{fs_panel},&H0000FF00,&H000000FF,&H00000000,&HC8202018,-1,0,0,0,100,100,0,0,3,6,0,2,60,60,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = [head]

    def ev(start, end, style, text, layer=0):
        lines.append(
            f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},"
            f"{style},,0,0,0,,{text}")

    total = segs[-1].audio_t + _eff(segs[-1]) if segs else 0.0
    windows = qso_windows(qsos, segs, offset_h, total, qso_times)
    scores = running_score(qsos)

    # --- header (callsign / contest / running score) -- static text plus a
    # running "NQ Mpts". Updates when a QSO *finishes*, not when its panel
    # first appears: points are for a completed contact, not one still
    # being worked. "Finishes" means the QSO's own qso_times entry (exact,
    # from the input log) when known -- that's windows[i][1], see
    # qso_windows. Without one for a given QSO there's no real finish time
    # to use at all, and windows[i][1] there is just "next QSO's start" (or
    # `total` for the last QSO, leaving no room to show its own score) --
    # so that QSO's trigger falls back to its panel's own start instead,
    # the same as before finish-tracking existed.
    header_base = f"{_esc(mycall)}  {_esc(mywwl)}   {_esc(contest)}"
    if not qsos:
        ev(0, total, 'Header', header_base)
    else:
        triggers = [windows[i][1] if (qso_times and qso_times[i] is not None) else windows[i][0]
                   for i in range(len(qsos))]
        if triggers[0] > 0:
            ev(0, triggers[0], 'Header', header_base)
        for i in range(len(qsos)):
            start = triggers[i]
            end = triggers[i + 1] if i + 1 < len(triggers) else total
            if end <= start:
                continue
            count, pts = scores[i]
            ev(start, end, 'Header', f"{header_base}   {count}Q {pts}pts")

    # --- rig/rotator state (top-left): timed per build_state_events, which
    # sub-divides within a WAV segment wherever telemetry itself shows the
    # state actually changing (see its docstring). No event at all when ptt
    # is unknown for that stretch.
    if state_events is not None:
        for start, end, st in state_events:
            if st.ptt is None:
                continue
            hexcol = STATE_TX_HEX if st.ptt else STATE_RX_HEX
            label = 'TX' if st.ptt else 'RX'
            text = f"{{\\c&H{hexcol}&}}● {label}"
            info = _fmt_rig_info(st.freq_hz, st.mode, st.az)
            if info:
                text += f"\\N{{\\c&HFFFFFF&}}{_esc(info)}"
            ev(start, end, 'State', text)

    # --- decode ticker: rolling window, flushed at the start of every fresh
    # burst of on-air activity -- not at a QSO's EDI timestamp, which is
    # only minute-precision and would flush mid-over. Trusted CW content
    # comes from two sources, merged into one chronological list of
    # (start, end, events) chunks: segments decoded whole (dur <= MAX_OVER_S,
    # each producing one chunk), and telemetry-confirmed CW sub-ranges
    # recovered from an otherwise-too-long segment we only listened to (see
    # decode_long_segment) -- possibly several per segment, since we may
    # have followed more than one on-air exchange without ever transmitting
    # ourselves. Segments telemetry confirms were *not* CW are skipped
    # outright: the decoder runs blind on every segment (there's no way to
    # know the mode in advance) and gate_events rejects most non-CW noise,
    # but a strong tone in voice audio can occasionally still slip through
    # trusted -- telemetry's own mode is ground truth where we have it.
    # Flushing is decided uniformly across all chunks by the real time gap
    # since the previous one (> MAX_OVER_S -- the same threshold used
    # everywhere else to tell a genuine over from a genuine gap), rather
    # than per-segment bookkeeping: two CW sub-ranges recovered from the
    # *same* long segment (e.g. two separate exchanges we listened in on)
    # are otherwise indistinguishable from one continuous burst.
    chunks: list[tuple[float, float, list[CharEvent]]] = []
    for s in segs:
        mode = _mode_at(s.audio_t, state_events) if state_events is not None else None
        if s.events and (mode is None or mode == 'CW'):
            chunks.append((s.audio_t, s.audio_t + _eff(s), s.events))
    chunks.extend(long_cw_spans or [])
    chunks.sort(key=lambda c: c[0])

    stream: list[tuple[float, str, bool]] = []   # (t, ch, flush_before)
    prev_end: float | None = None
    for start, end, events in chunks:
        is_burst_start = prev_end is None or start - prev_end > MAX_OVER_S
        if not is_burst_start and stream:
            stream.append((start, ' ', False))   # gap between overs, same burst
        for j, e in enumerate(events):
            stream.append((start + e.t, e.ch, is_burst_start and j == 0))
        prev_end = end
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
    for i, q in enumerate(qsos):
        start, end = windows[i]
        tag = '  \\N{\\c&H7070FF&}*** DUPE (0 pts) ***' if q.dup else ''
        style = 'PanelDup' if q.dup else 'Panel'
        txt = (f"QSO {i + 1}/{len(qsos)}   {q.dt.strftime('%H:%MZ')}"
               f"\\N{_esc(q.call)}   {_esc(q.loc)}   {q.pts} km"
               f"\\NTX {_esc(q.rst_s)} {_esc(q.nr_s)}"
               f"    RX {_esc(q.rst_r)} {_esc(q.nr_r)}{tag}")
        ev(start, end, style, txt, layer=1)

    # --- typewriter: what the operator was typing into the logger, bottom
    # center -- bright green like the logger's own TX line, since both mark
    # the operator's own action rather than something heard on the air.
    for start, end, text in (input_events or []):
        ev(start, end, 'Input', f"► {_esc(text)}")

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
RENDER_FPS = 30          # output frame rate; the webcam PiP is resampled to
                         # this too (see render) so both branches share one
                         # real-time clock


def render(wav: str, ass: str, out: str, W: int, H: int,
          webcam: str | None = None, webcam_start: float = 0.0) -> None:
    ass_esc = ass.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
    # Full-screen scrolling waterfall, dimmed to ~half luma so it reads as an
    # ambient background and the text stays crisp on top. overlap=0.8 makes it
    # scroll fast enough to fill the frame within the first few seconds.
    fchain = (
        f"[0:a]showspectrum=s={W}x{H}:mode=combined:slide=scroll:overlap=0.8:"
        f"color=intensity:scale=cbrt:fscale=log:saturation=1.6,"
        f"lutyuv=y=val*0.42,format=yuv420p,fps={RENDER_FPS}[bg];"
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
        pip_w = round(W * PIP_WIDTH_FRAC)
        margin = round(W * PIP_MARGIN_FRAC)
        cmd += ['-itsoffset', f'{webcam_start:.3f}', '-i', webcam]
        fchain += (
            f";[1:v]fps={RENDER_FPS},scale={pip_w}:-2,hflip,"
            f"tpad=stop_mode=clone:stop_duration=99999[pip]"
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
    ap.add_argument('edi', nargs='+',
                    help='EDI log(s) for the same session -- pass more than one '
                         'to merge multiple bands worked in one recording')
    ap.add_argument('-o', '--out', default='contest_video.mp4')
    ap.add_argument('--pitch', type=float, default=600.0, help='CW tone Hz')
    ap.add_argument('--res', choices=RESOLUTIONS, default='1080p')
    ap.add_argument('--contest', default='URH OB 2026 - CW')
    ap.add_argument('--skip-gaps', action='store_true',
                    help=f'trim silent gaps between QSOs to {GAP_KEEP_S:.0f}s each')
    ap.add_argument('--keep-ass', action='store_true',
                    help='keep intermediate .ass/.wav for inspection')
    ap.add_argument('--telemetry',
                    help='puskas_logger *-telemetry.jsonl -- optional: the RX/TX + QRG/mode '
                         'badge already comes from the WAV files\' own IC-9700 metadata; this '
                         'only adds bearing (ROT) and refines QRG/mode within long segments '
                         'where the operator QSY\'d with nothing to split the WAV on')
    ap.add_argument('--duration', type=float,
                    help='trim to the first DURATION seconds of real session time '
                         '(chronological preview; also skips CW-decoding past the '
                         'cutoff, so a short preview is much faster to build)')
    ap.add_argument('--webcam',
                    help='picture-in-picture selfie/webcam clip, synced automatically '
                         'from its own filename timestamp (e.g. VID_20260706_180003.mp4)')
    ap.add_argument('--input-log',
                    help="puskas_logger *-input.jsonl for a 'typewriter' overlay of what "
                         "was typed into the logger -- optional, older recordings won't "
                         "have one")
    ap.add_argument('--seed-input-log',
                    help="write a hand-editable 'qso' event skeleton to this path, one line "
                         "per QSO in the EDI(s) with a placeholder timestamp, then exit "
                         "without rendering -- for a recording made before --input-log "
                         "existed: edit each 't' against the audio, then pass the result "
                         "back in as --input-log for exact QSO-panel timing with no "
                         "cluster-snapping heuristics involved")
    args = ap.parse_args()

    if args.seed_input_log:
        _, _, qsos_all = merge_edi(args.edi)
        with open(args.seed_input_log, 'w') as fh:
            for q in qsos_all:
                fh.write(json.dumps({
                    't': q.dt.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z',
                    'event': 'qso',
                    'call': q.call,
                    'nr_s': q.nr_s,
                    'loc': q.loc,
                    'dup': q.dup,
                }) + '\n')
        print(f"wrote {len(qsos_all)} seed 'qso' events to {args.seed_input_log}")
        print("each 't' is just the EDI's own minute, seconds zeroed -- edit it to the "
              "QSO's real time from the audio, then pass "
              f"--input-log {args.seed_input_log} when rendering")
        return

    W, H = RESOLUTIONS[args.res]
    segs = scan_segments(args.recdir)
    if not segs:
        sys.exit(f"no timestamped WAVs found in {args.recdir}")
    print(f"{len(segs)} segments, {segs[-1].audio_t + segs[-1].dur:.0f}s audio")

    mycall, mywwl, qsos_all = merge_edi(args.edi)
    offset_h = derive_utc_offset(segs, qsos_all)
    print(f"{mycall} {mywwl}: {len(qsos_all)} QSOs, UTC+{offset_h} local")

    webcam_start = None
    if args.webcam:
        cam_wall = parse_webcam_wall(args.webcam)
        cam_dur = _ffprobe_duration(args.webcam)
        webcam_start = sync_webcam_start(cam_wall, cam_dur, qsos_all, segs, offset_h)
        print(f"  webcam: synced to start at {webcam_start:.0f}s in the output")

    if args.duration:
        segs = trim_to_duration(segs, args.duration)
        print(f"  duration: preview cut to first {args.duration:.0f}s "
              f"({len(segs)} segments)")

    read_wav_metadata(segs)
    known_wav = sum(1 for s in segs if s.ptt is not None)
    print(f"  WAV metadata: {known_wav}/{len(segs)} segments have IC-9700 rig tags")

    telemetry = load_telemetry(args.telemetry) if args.telemetry else []
    state_events = build_state_events(segs, telemetry, offset_h)
    known = sum(1 for _, _, st in state_events if st.ptt is not None)
    suffix = f" ({args.telemetry} refines freq/mode within long segments)" if args.telemetry else ""
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
    decoded = sum(len(s.events) for s in segs) + sum(len(ev) for _, _, _, ev in long_cw_raw)
    trusted_overs = sum(1 for s in segs if s.events) + len(long_cw_raw)
    print(f"  {decoded} characters from {trusted_overs} trusted overs")
    if long_cw_raw:
        print(f"  including {len(long_cw_raw)} CW exchange(s) recovered from "
              f"otherwise-too-long listening segments")

    if args.skip_gaps:
        long_cw_segs = {id(s) for s, _, _, _ in long_cw_raw}
        remap_audio_t(segs, long_cw_segs)
        total = segs[-1].audio_t + _eff(segs[-1])
        print(f"  skip-gaps: {total:.0f}s video (was {segs[-1].audio_t + segs[-1].dur:.0f}s)")

    total = segs[-1].audio_t + _eff(segs[-1])
    qsos = [q for q in qsos_all
            if audio_time_for(q.dt + timedelta(hours=offset_h), segs) < total]
    if len(qsos) < len(qsos_all):
        print(f"  {len(qsos)}/{len(qsos_all)} QSOs fall within the {total:.0f}s cut")

    if webcam_start is not None and webcam_start >= total:
        print("  webcam starts after the cut ends -- dropping the PiP overlay")
        webcam_start = None

    # Resolved to absolute video-timeline time only now, using each
    # segment's final audio_t (post-remap, if --skip-gaps was used).
    long_cw_spans = [(seg.audio_t + t0, seg.audio_t + t1, events)
                     for seg, t0, t1, events in long_cw_raw]

    input_events = None
    qso_times = None
    if args.input_log:
        input_log = load_input_log(args.input_log)
        input_events = build_input_events(input_log, segs, offset_h, total)
        print(f"  input log: {len(input_events)} typed states from {args.input_log}")
        qso_times = match_qso_times(qsos, input_log)
        matched = sum(1 for t in qso_times if t is not None)
        print(f"  {matched}/{len(qsos)} QSOs got an exact submit time from the input log")

    ass_text = build_ass(segs, qsos, mycall, mywwl, args.contest,
                         offset_h, W, H, state_events, input_events, qso_times,
                         long_cw_spans=long_cw_spans)
    ass_path = os.path.splitext(args.out)[0] + '.ass'
    with open(ass_path, 'w') as fh:
        fh.write(ass_text)

    stem = os.path.splitext(args.out)[0]
    windows = qso_windows(qsos, segs, offset_h, total, qso_times)
    with open(stem + '.chapters.txt', 'w') as fh:
        fh.write(build_chapters(qsos, windows))
    with open(stem + '.srt', 'w') as fh:
        fh.write(build_srt(qsos, windows))
    print(f"wrote {stem}.chapters.txt and {stem}.srt")

    wav = os.path.splitext(args.out)[0] + '.concat.wav'
    print("concatenating audio ...")
    concat_audio(segs, wav)
    print("rendering (this takes a while) ...")
    render(wav, ass_path, args.out, W, H,
          webcam=args.webcam if webcam_start is not None else None,
          webcam_start=webcam_start or 0.0)

    if not args.keep_ass:
        os.remove(wav)
        os.remove(ass_path)
    print(f"wrote {args.out}")


if __name__ == '__main__':
    main()
