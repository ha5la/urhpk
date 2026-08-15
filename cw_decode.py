"""CW decoding: audio in, timed characters out.

A segment's audio is demodulated to an envelope at the detected pitch,
thresholded with hysteresis into keying on/off runs, and those runs are read
as Morse against a dit length estimated from the segment itself. Nothing here
knows about video, ffmpeg or the EDI log.

The trust gate (gate_events) is the other half: a single-tone decoder turns
band noise into gibberish, so a decode is only shown once it looks like a real
over.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from wav import read_wav_range

if TYPE_CHECKING:
    from rig_state import SegState
    from timeline import Segment


MORSE = {
    ".-": "A",
    "-...": "B",
    "-.-.": "C",
    "-..": "D",
    ".": "E",
    "..-.": "F",
    "--.": "G",
    "....": "H",
    "..": "I",
    ".---": "J",
    "-.-": "K",
    ".-..": "L",
    "--": "M",
    "-.": "N",
    "---": "O",
    ".--.": "P",
    "--.-": "Q",
    ".-.": "R",
    "...": "S",
    "-": "T",
    "..-": "U",
    "...-": "V",
    ".--": "W",
    "-..-": "X",
    "-.--": "Y",
    "--..": "Z",
    "-----": "0",
    ".----": "1",
    "..---": "2",
    "...--": "3",
    "....-": "4",
    ".....": "5",
    "-....": "6",
    "--...": "7",
    "---..": "8",
    "----.": "9",
    ".-.-.-": ".",
    "--..--": ",",
    "..--..": "?",
    "-..-.": "/",
    "-...-": "=",
    ".-.-.": "+",
    "-....-": "-",
    "-.-.--": "!",
}

ENV_FS = 200  # envelope sample rate (Hz) after demodulation
LOWPASS_CUTOFF_HZ = 120.0  # envelope filter cutoff -- covers real CW keying bandwidth
LOWPASS_NTAPS = 321  # windowed-sinc length; longer than the old boxcar for a
# much sharper stopband (rejects moderate-offset QRM
# noticeably better -- verified against real recordings)
THR_HI_FRAC = 0.35  # hysteresis: fraction of (peak-floor) to trigger "on"
THR_LO_FRAC = 0.15  # hysteresis: fraction of (peak-floor) to release back to "off"
DEBOUNCE_DIT_FRAC = 0.5  # on/off runs shorter than this fraction of the segment's
# own preliminary dit estimate are noise, not real keying
# -- merged into their neighbour (see _debounce_on)

# The trust gate's four thresholds.
MAX_OVER_S = 35.0  # a real over is short; longer is a listening period.
# Unlike FREQ_MATCH_TOLERANCE_HZ there is no clean statistical
# gap to sit in -- real segment durations run continuously from
# 30s past 100s -- so this is an evidence-backed nudge (was
# 30.0) for one confirmed 32.5s exchange with a full locator,
# not a broad guess. The other three gates still cover a
# listening period that happens to land in 30-35s.
MIN_SNR_DB = 20.0  # reject weak noise-only segments
MIN_QUALITY = 0.5  # reject text dominated by isolated single letters (noise)
MAX_DOMINANCE = 0.4  # reject text where one letter dominates (chopped carrier)
MIN_CHARS_FOR_DOMINANCE = 5  # below this length, dominance is structurally
# high regardless of content -- see _dominance


def _quality(text: str) -> float:
    """Fraction of whitespace tokens longer than one char. Noise decodes to a
    stream of single letters (E/T/I/S); real overs to callsigns and reports."""
    toks = [t for t in text.split(" ") if t]
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
    shows up over many characters in practice anyway (see tests/test_cw_decode.py)."""
    chars = [c for c in text if c != " "]
    if len(chars) < MIN_CHARS_FOR_DOMINANCE:
        return 0.0
    return max(chars.count(c) for c in set(chars)) / len(chars)


def gate_events(
    dur: float, events: list[CharEvent], snr: float, check_duration: bool = True
) -> list[CharEvent]:
    """Return events if the segment is a trustworthy over, else [].

    check_duration=False skips the MAX_OVER_S check -- for telemetry-
    confirmed CW sub-ranges extracted from an otherwise-too-long segment
    (see decode_cw_subranges), where the duration gate's usual purpose --
    rejecting a segment whose unexplained length makes it suspicious --
    doesn't apply: telemetry mode confirmation is already stronger evidence
    than length that this specific span is genuine CW, not noise."""
    text = "".join(e.ch for e in events)
    if (
        (not check_duration or dur < MAX_OVER_S)
        and snr >= MIN_SNR_DB
        and _quality(text) >= MIN_QUALITY
        and _dominance(text) <= MAX_DOMINANCE
    ):
        return events
    return []


@dataclass
class CharEvent:
    t: float  # seconds, relative to segment start
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
    trusting a single assumed pitch for the whole round.

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
    env = np.abs(np.convolve(iq, h, "same"))
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


def _decode_samples(
    x: np.ndarray, sr: int, pitch: float = 600.0
) -> tuple[list[CharEvent], float]:
    """Decode a raw sample buffer into timed characters and its SNR in dB --
    the actual demod/hysteresis/debounce/decode pipeline, factored out of
    decode_segment so decode_cw_subranges (see below) can run the same
    pipeline on an extracted sub-range of a WAV file instead of always the
    whole thing.

    `pitch` is only a fallback for the rare case _detect_pitch can't find
    anything (e.g. a silent segment) -- the actual demodulation frequency
    is always auto-detected, see _detect_pitch's docstring for why a single
    assumed pitch for the whole round doesn't hold.

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
    sym = ""
    sym_start = 0.0
    for s, d, idx in runs:
        t0 = idx / efs
        u = d / dit
        if s:
            if not sym:
                sym_start = t0
            sym += "." if u < 2.0 else "-"
        else:
            if u >= 2.0 and sym:  # end of character
                ch = MORSE.get(sym, "")
                if ch:
                    events.append(CharEvent(sym_start, ch))
                sym = ""
            if u >= 5.0:  # word gap
                if events and events[-1].ch != " ":
                    events.append(CharEvent(t0, " "))
    if sym:
        ch = MORSE.get(sym, "")
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
        # segment, which decode_cw_subranges (below) handles separately by
        # extracting and decoding just that sub-range.
        w.close()
        return [], 0.0
    x = np.frombuffer(w.readframes(n_frames), dtype=np.int16).astype(float)
    w.close()
    return _decode_samples(x, sr, pitch)


def cw_subranges(
    seg: Segment, state_events: list[tuple[float, float, SegState]]
) -> list[tuple[float, float]]:
    """Telemetry-confirmed CW-mode time ranges within `seg`'s own span,
    expressed as (start, end) offsets in seconds relative to the segment's
    own start (0..seg.dur) -- deliberately not absolute video-timeline
    seconds, so the result stays valid even if audio_t is later remapped
    (see decode_cw_subranges and remap_audio_t's cw_span_segs parameter).

    Only meaningful for a segment too long to decode as a whole (see
    decode_cw_subranges): our own recorder only splits a new WAV file on
    our own PTT, so a segment where we just listened to someone else's
    entire exchange -- possibly spanning several of their own mode changes
    -- stays one long file. state_events (from build_state_events) already
    carries the right sub-division for this, seeded from the WAV's own
    starting mode and refined by telemetry wherever it shows a genuine
    change within the segment.

    A state run also ends at every retune, so one continuous CW period
    arrives as several touching runs and is joined back into one range
    here. The join is not cosmetic: each range is decoded on its own, and
    _estimate_dit needs several ON runs to estimate a dit from, which a
    one-second sliver of a real over does not have."""
    seg_start, seg_end = seg.audio_t, seg.audio_t + seg.dur
    out: list[tuple[float, float]] = []
    for start, end, st in state_events:
        if st.mode != "CW":
            continue
        s0, s1 = max(start, seg_start), min(end, seg_end)
        if s1 <= s0:
            continue
        if out and s0 <= out[-1][1]:
            out[-1] = (out[-1][0], s1)
        else:
            out.append((s0, s1))
    return [(s0 - seg.audio_t, s1 - seg.audio_t) for s0, s1 in out]


def decode_cw_subranges(
    seg: Segment,
    state_events: list[tuple[float, float, SegState]],
    pitch: float = 600.0,
) -> list[tuple[float, float, list[CharEvent]]]:
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
        x, sr = read_wav_range(seg.path, t0, t1)
        events, snr = _decode_samples(x, sr, pitch)
        events = gate_events(t1 - t0, events, snr, check_duration=False)
        if events:
            out.append((t0, t1, events))
    return out


def decode_round(
    segs: list[Segment],
    state_events: list[tuple[float, float, SegState]],
    pitch: float = 600.0,
) -> list[tuple[Segment, float, float, list[CharEvent]]]:
    """Decode every segment of a round, populating each one's `.events` and
    returning the sub-ranges recovered from the segments too long to decode
    as a whole.

    Segments longer than MAX_OVER_S are never decoded as a whole (see
    decode_segment) -- but one can still contain a real CW exchange between
    *other* stations that we only listened to, with no PTT of our own to
    split the file on. decode_cw_subranges recovers those from state_events'
    telemetry-confirmed CW sub-ranges. Offsets stay segment-relative (t0, t1)
    rather than resolved to absolute video-timeline time here, so they stay
    valid even if remap_audio_t (--skip-gaps) later shifts audio_t."""
    out: list[tuple[Segment, float, float, list[CharEvent]]] = []
    for s in segs:
        if s.dur > MAX_OVER_S:
            for t0, t1, events in decode_cw_subranges(s, state_events, pitch):
                out.append((s, t0, t1, events))
            continue
        events, snr = decode_segment(s.path, pitch)
        s.events = gate_events(s.dur, events, snr)
    return out
