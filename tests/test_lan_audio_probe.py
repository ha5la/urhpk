"""Tests for the LAN audio probe's sample-continuity detector.

The probe's conclusion is whatever `lag_profile` reports, so the detector has
to be shown finding a slip that was deliberately put there -- a run of
"CONTINUOUS" proves nothing about a detector never seen to fire.
"""

from __future__ import annotations

import numpy as np
import pytest

from lan_audio_probe import coarse_lag, lag_profile

RATE = 16000


def _noise(n: int, seed: int = 7) -> np.ndarray:
    # Squelch hiss is what these captures actually contain, and broadband noise
    # is the easiest thing in the world to correlate: every window is unique.
    # Padding must use its own seed -- with one shared seed the padding is a
    # prefix of the signal, and lag 0 becomes a genuinely correct answer.
    return np.random.default_rng(seed).standard_normal(n).astype(np.float32)


def test_reports_a_constant_lag_for_two_copies_of_the_same_audio():
    source = _noise(RATE * 20)
    lan = np.concatenate([_noise(RATE * 2, 1), source])  # LAN starts 2 s earlier
    rows = lag_profile(lan, source, RATE, probes=8)
    assert [r[1] for r in rows] == [RATE * 2] * len(rows)
    assert min(r[2] for r in rows) > 0.99


def test_finds_a_single_dropped_sample_halfway_through():
    source = _noise(RATE * 20)
    half = RATE * 10
    # The LAN stream loses exactly one sample at t=10s: everything after it
    # sits one sample early, which no packet-pacing measurement could see.
    lan = np.concatenate([source[:half], source[half + 1 :]])
    rows = lag_profile(lan, source, RATE, probes=8)
    lags = [r[1] for r in rows]
    assert set(lags) == {0, -1}
    assert lags[0] == 0 and lags[-1] == -1


def test_finds_a_burst_of_inserted_samples():
    source = _noise(RATE * 20)
    half = RATE * 10
    lan = np.concatenate([source[:half], _noise(500, 2), source[half:]])
    rows = lag_profile(lan, source, RATE, probes=8)
    lags = [r[1] for r in rows]
    assert lags[0] == 0
    assert lags[-1] == 500


def test_reports_low_correlation_for_unrelated_audio():
    rows = lag_profile(_noise(RATE * 20), _noise(RATE * 10) * -1.0, RATE, probes=4)
    # different seeds would be ideal, but the same noise negated is the harder
    # case: perfectly anti-correlated, so a detector taking |correlation| would
    # match it at 1.0 and call two unrelated streams aligned.
    assert max(r[2] for r in rows) < 0.5


def test_window_and_search_are_in_seconds_not_samples():
    source = _noise(RATE * 6)
    lan = np.concatenate([_noise(RATE // 2, 1), source])
    rows = lag_profile(lan, source, RATE, win_s=0.5, search_s=1.0, probes=4)
    assert [r[1] for r in rows] == [RATE // 2] * len(rows)


@pytest.mark.parametrize("offset", [0, 137, 4001])
def test_recovers_any_offset_within_the_search_window(offset):
    source = _noise(RATE * 10)
    lan = np.concatenate([_noise(offset, 3), source]) if offset else source
    rows = lag_profile(lan, source, RATE, probes=4)
    assert [r[1] for r in rows] == [offset] * len(rows)


def test_coarse_lag_finds_a_start_offset_far_beyond_the_probe_search():
    # The real captures were started by hand ~9 s apart, well outside any
    # per-probe search window worth using for slip detection.
    source = _noise(RATE * 60)
    lan = source[RATE * 9 :]  # LAN begins 9 s into the SD recording
    lag, corr = coarse_lag(lan, source, RATE)
    assert lag == -RATE * 9
    assert corr > 0.99


def test_lag_profile_reports_relative_to_the_base_lag():
    source = _noise(RATE * 30)
    lan = source[RATE * 5 :]
    rows = lag_profile(
        lan, source[: RATE * 20], RATE, search_s=0.5, base_lag=-RATE * 5, probes=6
    )
    assert [r[1] for r in rows] == [0] * len(rows)


def test_a_slip_is_still_found_once_a_base_lag_is_applied():
    source = _noise(RATE * 40)
    half = RATE * 20
    slipped = np.concatenate([source[:half], source[half + 1 :]])
    lan = slipped[RATE * 5 :]
    rows = lag_profile(
        lan, source[: RATE * 30], RATE, search_s=0.5, base_lag=-RATE * 5, probes=8
    )
    assert set(r[1] for r in rows) == {0, -1}


def test_skips_probes_that_fall_before_the_capture_started():
    # The SD card is running before the LAN capture connects, so the first
    # probes have no counterpart at all. Slicing them out with a negative stop
    # index silently matched the far end of the capture instead.
    source = _noise(RATE * 30)
    lan = source[RATE * 9 :]
    rows = lag_profile(lan, source, RATE, search_s=0.5, base_lag=-RATE * 9, probes=10)
    assert rows, "later probes should still match"
    assert all(r[1] == 0 for r in rows)
    assert all(r[2] > 0.99 for r in rows)
