"""Tests for the spectrum-scope waterfall background."""

from datetime import datetime, timezone

import numpy as np

import contest_video as cv
from icom_net import write_scope_record
from scope_render import (
    SCOPE_AMP_MAX,
    _resize_scope_row,
    _scope_colormap,
    render_scope_video,
)
from timeline import (
    Segment,
)
from webcam_sync import WebcamClip


def _epoch(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


class TestScopeFreqPeriods:
    def _segs(self):
        return [
            Segment("a", datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
        ]


class TestScopeColormap:
    def test_colormap_shape_and_endpoints(self):
        lut = _scope_colormap()
        assert lut.shape == (SCOPE_AMP_MAX + 1, 3)
        assert tuple(int(c) for c in lut[0]) == (0, 0, 0)
        assert tuple(int(c) for c in lut[SCOPE_AMP_MAX]) == (255, 0, 0)

    def test_resize_scope_row_preserves_length_and_endpoints(self):
        pixels = np.array([10, 50, 90], dtype=np.uint8)
        resized = _resize_scope_row(pixels, 6)
        assert len(resized) == 6
        assert resized[0] == 10
        assert resized[-1] == 90


class TestRenderScopeVideoTiming:
    def test_rows_scroll_on_fixed_time_grid_not_per_sweep(self, monkeypatch, tmp_path):
        # Regression test for a real reported issue: rows used to scroll one
        # per real sweep, so the canvas height represented however many
        # seconds happened to fit at the recording's actual sweep rate
        # (hundreds of seconds in one real synthetic test), not the radio's
        # own ~10s-per-screen-height waterfall speed. Verifies the fix
        # directly: a slow sweep rate (1/s here) against a faster row rate
        # (rows/span_s = 4/2 = 2/s) must hold each row on screen for
        # multiple output frames (compression/holding), not scroll a fresh
        # row in on every single output frame regardless of real data rate.
        # Pixel values deliberately avoid 0: lut[0] is pure black (0,0,0),
        # identical to an untouched canvas row -- using it here would make a
        # "held-over" row indistinguishable from "never reached yet",
        # silently defeating the one assertion below that actually tells
        # the fixed and buggy behaviour apart.
        scope_path = tmp_path / "t.scope"
        with open(scope_path, "wb") as f:
            write_scope_record(f, 1000.0, 144_000_000, 146_000_000, bytes([40]))
            write_scope_record(f, 1001.0, 144_000_000, 146_000_000, bytes([160]))

        frames = []

        class FakeStdin:
            def write(self, data):
                frames.append(np.frombuffer(data, dtype=np.uint8).reshape(4, 2, 3))

            def close(self):
                pass

        class FakeProc:
            stdin = FakeStdin()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda cmd, stdin=None: FakeProc())
        render_scope_video(
            str(scope_path), str(tmp_path / "out.mp4"), W=2, H=4, fps=2, span_s=2.0
        )

        assert len(frames) == 3  # t=0.0, 0.5, 1.0 -- duration=1.0s, frame_dt=0.5s
        lut = _scope_colormap()

        first = frames[0]
        assert (first[0] == lut[40]).all()
        assert (first[1:] == 0).all()  # canvas not yet filled below the first row

        last = frames[-1]
        assert (last[0] == lut[160]).all()  # newest row (sweep @ t=1.0) enters at top
        # The key assertion: row 2 must ALSO show the held-over sweep@40
        # value, not still be untouched black -- the row-rate here (2/s) is
        # faster than the real sweep rate (1/s), so the fixed time grid
        # must duplicate the same sweep across two physical canvas rows
        # (indices 1 and 2) to keep scrolling at a constant rate. The old,
        # buggy per-sweep scrolling only ever pushed two rows total for
        # these two sweeps, leaving row 2 untouched (black) at this point --
        # confirmed by temporarily reverting to that logic and observing
        # this exact assertion fail (row 2 was 0, not lut[40]) before
        # restoring the fix.
        assert (last[1] == lut[40]).all()
        assert (last[2] == lut[40]).all()
        assert (last[3] == 0).all()  # canvas still not fully filled by t=1.0

    def test_a_stalled_stream_draws_black_rows_not_a_held_one(
        self, monkeypatch, tmp_path
    ):
        # Sweeps 2s apart against a 1s stall threshold: the rows within
        # SCOPE_STALL_S of a sweep hold it, the ones past it go black, so a
        # blackout reads as a gap in the waterfall instead of a smear.
        scope_path = tmp_path / "t.scope"
        with open(scope_path, "wb") as f:
            write_scope_record(f, 1000.0, 144_000_000, 146_000_000, bytes([40]))
            write_scope_record(f, 1002.0, 144_000_000, 146_000_000, bytes([160]))

        frames = []

        class FakeStdin:
            def write(self, data):
                frames.append(np.frombuffer(data, dtype=np.uint8).reshape(4, 2, 3))

            def close(self):
                pass

        class FakeProc:
            stdin = FakeStdin()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda cmd, stdin=None: FakeProc())
        render_scope_video(
            str(scope_path), str(tmp_path / "out.mp4"), W=2, H=4, fps=2, span_s=2.0
        )

        lut = _scope_colormap()
        last = frames[-1]
        assert (last[0] == lut[160]).all()  # the sweep at t=2.0
        assert (last[1] == 0).all()  # t=1.5, 1.5s since the last sweep
        assert (last[2] == lut[40]).all()  # t=1.0, still inside the threshold
        assert (last[3] == lut[40]).all()  # t=0.5


class TestRenderScopeBackground:
    def test_scope_branch_overlays_onto_the_audio_background(
        self, monkeypatch, tmp_path
    ):
        captured = {}

        def fake_run(cmd, check=True):
            captured["cmd"] = cmd

        monkeypatch.setattr(cv.subprocess, "run", fake_run)
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1920,
            1080,
            scope=str(tmp_path / "scope.mp4"),
            scope_start=5.0,
            scope_end=50.0,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        assert "[1:v]scale=1920:1080" in fchain
        assert "enable='between(t,5.000,50.000)'" in fchain
        # The scope overlay replaces the audio-derived background, so it is
        # the layer the PiPs and HUD sit on -- it must not be composited on
        # top of them.
        assert "[specbg][scopebg]overlay=" in fchain

    def test_scope_shifts_cast_and_webcam_input_indices(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, check=True):
            captured["cmd"] = cmd

        monkeypatch.setattr(cv.subprocess, "run", fake_run)
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1920,
            1080,
            scope=str(tmp_path / "scope.mp4"),
            scope_start=0.0,
            scope_end=10.0,
            cast=str(tmp_path / "cast.mp4"),
            cast_start=1.0,
            webcams=[WebcamClip(str(tmp_path / "cam.mp4"), 2.0)],
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        # scope=input 1, cast=input 2, webcam=input 3 -- confirms indices
        # shift correctly to make room for scope ahead of the existing ones.
        assert "[1:v]scale=1920:1080" in fchain
        assert "[2:v]setpts=" in fchain
        assert "[3:v]setpts=" in fchain

    def test_no_scope_leaves_the_audio_background_alone(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, check=True):
            captured["cmd"] = cmd

        monkeypatch.setattr(cv.subprocess, "run", fake_run)
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1920,
            1080,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        assert "scopebg" not in fchain
