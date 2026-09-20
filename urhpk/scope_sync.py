"""Lining the radio's own spectrum scope up with the radio's own audio.

The scope is the one stream stamped by neither clock that matters: each sweep
carries `time.time()` from the moment it *arrived* over the LAN, so it lags the
signal it draws by the radio's own processing plus the network path, and then
lags further as the laptop's clock walks against the sample clock the audio was
recorded on. Both are measurable from the recording itself, without asking the
operator for anything: a keyed signal draws the same shape on the scope as it
makes in the audio, so the amplitude in the bins around a segment's own QRG can
be correlated against that segment's audio envelope, exactly as
refine_webcam_start correlates two microphones' envelopes.
"""

from __future__ import annotations

import numpy as np

from urhpk.timeline import Segment
from urhpk.wav import read_wav_range
from urhpk.webcam_sync import _rms_envelope, envelope_offset, robust_line_fit

# Modes whose envelope moves: CW keys on and off, SSB follows speech. FM
# carries a constant carrier whatever the audio does, so its trace is a flat
# line against a modulated envelope and correlates with nothing.
KEYED_MODES = ("CW", "CW-R", "USB", "LSB")

SCOPE_QRG_BINS = 2  # bins either side of the tuned frequency summed into the
# trace: the signal is a few bins wide at a 100 kHz span, and the exact centre
# depends on the mode's own offset from the carrier


def scope_trace(
    records: list[tuple[float, int, int, bytes]],
    out_t: np.ndarray,
    grid: np.ndarray,
    freq_hz: int,
) -> np.ndarray:
    """The scope's amplitude at `freq_hz` sampled onto `grid` (output-time
    seconds), from sweeps whose own output times are `out_t`."""
    lo = int(np.searchsorted(out_t, grid[0]))
    hi = int(np.searchsorted(out_t, grid[-1], side="right"))
    if hi - lo < 3:
        return np.array([])
    amps = []
    for ts, start_hz, end_hz, pixels in records[lo:hi]:
        p = np.frombuffer(pixels, np.uint8)
        span = max(end_hz - start_hz, 1)
        c = int(round((freq_hz - start_hz) / span * (len(p) - 1)))
        if not 0 <= c < len(p):
            return np.array([])
        amps.append(float(p[max(0, c - SCOPE_QRG_BINS) : c + SCOPE_QRG_BINS + 1].max()))
    return np.interp(grid, out_t[lo:hi], amps)


def refine_scope_start(
    records: list[tuple[float, int, int, bytes]],
    segs: list[Segment],
    scope_start: float,
    max_anchors: int = 20,
    padding_s: float = 3.0,
    min_confidence: float = 0.3,
    env_win_s: float = 0.05,
) -> tuple[float, float, int]:
    """Where the scope recording really belongs on the output timeline, and
    the rate its own timeline needs stretching by.

    Anchors are RX segments in a keyed mode, long enough to carry a rhythm
    (the operator's own TX is no use: the scope is blanked while transmitting).
    Each one's audio envelope is matched against the scope's trace at that
    segment's QRG, and the per-anchor corrections are fitted as a line --
    intercept for the arrival lag, slope for the clock rate, the same shape of
    answer refine_webcam_start returns and for the same reason.

    Returns (corrected_scope_start, rate, n_confident_anchors), leaving
    scope_start alone when nothing correlated."""
    if len(records) < 2:
        return scope_start, 0.0, 0
    out_t = scope_start + (np.array([r[0] for r in records]) - records[0][0])
    candidates = [
        s
        for s in segs
        if not s.ptt and s.dur >= 6.0 and s.freq_hz and (s.mode or "") in KEYED_MODES
    ]
    step = max(1, len(candidates) // max_anchors)
    ts: list[float] = []
    corrections: list[float] = []
    for seg in candidates[::step]:
        audio, sr = read_wav_range(seg.path, 0.0, seg.dur)
        if len(audio) == 0:
            continue
        ref = _rms_envelope(audio, sr, env_win_s)
        if len(ref) < 3:
            continue
        grid = np.arange(
            seg.audio_t - padding_s,
            seg.audio_t + len(ref) * env_win_s + padding_s,
            env_win_s,
        )
        trace = scope_trace(records, out_t, grid, seg.freq_hz)
        if len(trace) == 0:
            continue
        correction, confidence = envelope_offset(ref, trace, padding_s, env_win_s)
        if confidence >= min_confidence:
            ts.append(seg.audio_t)
            corrections.append(correction)
    if not corrections:
        return scope_start, 0.0, 0
    intercept, rate = robust_line_fit(ts, corrections)
    return scope_start + intercept, rate, len(corrections)
