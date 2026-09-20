"""Tests for lining the radio's own scope sweeps up with the audio."""

from datetime import datetime, timezone

import numpy as np
import pytest

from urhpk.scope_sync import refine_scope_start
from urhpk.timeline import Segment, stream_start


def _sweep(ts, amp, freq_hz=144_300_000, span=100_000, bins=475):
    """One record as read_scope_records returns it, with `amp` in the bins
    around freq_hz and noise everywhere else."""
    pixels = np.full(bins, 5, dtype=np.uint8)
    centre = int((freq_hz - (freq_hz - span // 2)) / span * (bins - 1))
    pixels[centre - 2 : centre + 3] = amp
    return (ts, freq_hz - span // 2, freq_hz + span // 2, pixels.tobytes())


class TestRefineScopeStart:
    """The scope is stamped when each sweep reached the laptop, not when the
    radio swept -- so it runs late by the path between them, and by whatever
    the laptop's clock is doing against the radio's over a round."""

    EPOCH = datetime(2026, 9, 7, 16, 0, 0, tzinfo=timezone.utc).timestamp()
    SR = 8000

    def _round(self, lag, tmp_path, keying=(0, 1, 0, 1, 1, 0, 1, 0)):
        """A round whose scope shows `lag` seconds after the audio does.

        One RX segment per minute, each carrying the same 8 s of on/off
        keying -- CW, which is what makes a scope trace and an audio envelope
        the same shape.
        """
        import wave

        segs, records = [], []
        for i in range(6):
            wall = datetime(2026, 9, 7, 16, i, 0)
            path = str(tmp_path / f"{i}.wav")
            samples = np.concatenate(
                [
                    (np.random.default_rng(i).normal(0, 3000 if on else 20, self.SR))
                    for on in keying
                ]
            ).astype(np.int16)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.SR)
                w.writeframes(samples.tobytes())
            seg = Segment(path, wall, float(len(keying)), i * 60.0)
            seg.freq_hz, seg.mode, seg.ptt = 144_300_000, "CW", False
            segs.append(seg)
            for k, on in enumerate(keying):
                for j in range(20):  # ~20 sweeps/s
                    ts = self.EPOCH + i * 60 + k + j / 20 + lag
                    records.append(_sweep(ts, 120 if on else 8))
        return segs, sorted(records)

    def _placed(self, records, segs):
        """Where the renderer puts the recording before any of this runs: the
        first sweep's own stamp, which is already the lag late."""
        return stream_start(
            datetime.fromtimestamp(records[0][0], tz=timezone.utc).replace(tzinfo=None),
            segs,
        )

    def test_finds_the_lag_the_sweeps_arrive_with(self, tmp_path):
        segs, records = self._round(0.8, tmp_path)
        placed = self._placed(records, segs)
        assert placed == pytest.approx(0.8, abs=0.01)  # late, as recorded
        start, rate, n = refine_scope_start(records, segs, placed)
        assert n >= 4
        assert start == pytest.approx(0.0, abs=0.1)  # and pulled back onto it
        assert rate == pytest.approx(0.0, abs=0.001)

    def test_no_confident_anchors_leaves_the_start_alone(self, tmp_path):
        segs, records = self._round(0.8, tmp_path, keying=(1,) * 8)  # no rhythm
        start, rate, n = refine_scope_start(records, segs, 12.0)
        assert (start, rate, n) == (12.0, 0.0, 0)
