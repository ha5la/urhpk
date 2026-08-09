"""Tests for lining the webcam up with the radio audio, including drift."""

from datetime import datetime

import numpy as np

import contest_video as cv
import video_format
import webcam_sync
from timeline import (
    Qso,
    Segment,
    derive_utc_offset,
)
from webcam_sync import (
    _find_offset_correction,
    _rms_envelope,
    parse_webcam_precise_filename,
    parse_webcam_wall,
    refine_webcam_start,
    sync_webcam_start,
    webcam_start_from_log,
    webcam_start_wall,
)


class TestRenderWebcamSync:
    def test_pip_branch_resamples_to_render_fps(self, monkeypatch, tmp_path):
        # Regression test for a real reported bug: sync was correct at the
        # start of a rendered video but the audio read as over a second
        # late by the end. Root cause, confirmed against the real webcam
        # file's own packet timestamps: a phone recording can claim a
        # constant frame rate while its actual per-frame timestamps are
        # genuinely variable (thousands of scattered micro frame-drops
        # over a long capture) -- without resampling explicitly to
        # RENDER_FPS using the decoder's true PTS, the PiP branch runs
        # very slightly fast relative to the audio-driven main timeline.
        # render() shells out to ffmpeg, so this checks the constructed
        # command rather than actually invoking it.
        captured = {}

        def fake_run(cmd, check=True):
            captured["cmd"] = cmd

        monkeypatch.setattr(cv.subprocess, "run", fake_run)
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1920,
            1080,
            webcam=str(tmp_path / "cam.mp4"),
            webcam_start=10.0,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        pip_chain = fchain.split("[1:v]")[1].split("[pip]")[0]
        assert f"fps={video_format.RENDER_FPS}" in pip_chain

    def test_pip_branch_stretches_timeline_by_webcam_rate(self, monkeypatch, tmp_path):
        # Regression test for a real reported bug, separate from the frame-
        # drop one above: the phone and the radio recorder are independent
        # devices whose clocks don't tick at exactly the same *rate* -- a
        # linear drift that grew smoothly to several seconds over a ~2 hour
        # session, which a constant -itsoffset shift cannot correct (see
        # refine_webcam_start). setpts=PTS/(1-webcam_rate) stretches or
        # compresses the PiP's own timeline to compensate; it must run
        # *before* fps resamples onto a clean grid, so the resampling
        # itself uses the corrected timeline.
        captured = {}

        def fake_run(cmd, check=True):
            captured["cmd"] = cmd

        monkeypatch.setattr(cv.subprocess, "run", fake_run)
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1920,
            1080,
            webcam=str(tmp_path / "cam.mp4"),
            webcam_start=10.0,
            webcam_rate=0.0005,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        pip_chain = fchain.split("[1:v]")[1].split("[pip]")[0]
        assert pip_chain.startswith("setpts=PTS/0.9995")
        assert pip_chain.index("setpts=") < pip_chain.index(
            f"fps={video_format.RENDER_FPS}"
        )

    def test_webcam_pip_is_not_mirrored(self, monkeypatch, tmp_path):
        # The same-machine Alt+V capture records the laptop cam the right way
        # round; the old phone-only hflip must be gone.
        captured = {}
        monkeypatch.setattr(
            cv.subprocess, "run", lambda cmd, check=True: captured.update(cmd=cmd)
        )
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1280,
            720,
            webcam=str(tmp_path / "cam.mp4"),
            webcam_start=10.0,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        assert "hflip" not in fchain

    def test_cast_pip_is_slightly_transparent(self, monkeypatch, tmp_path):
        # The cast box blends over the waterfall via a lowered alpha.
        captured = {}
        monkeypatch.setattr(
            cv.subprocess, "run", lambda cmd, check=True: captured.update(cmd=cmd)
        )
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1280,
            720,
            cast=str(tmp_path / "cast.mp4"),
            cast_start=5.0,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        cast_chain = fchain.split("[1:v]")[1].split("[castpip]")[0]
        assert f"colorchannelmixer=aa={cv.CAST_PIP_ALPHA}" in cast_chain

    def test_cast_pip_stretches_timeline_by_cast_rate(self, monkeypatch, tmp_path):
        # The cast recording (asciinema) and the webcam capture are both
        # timestamped by the same laptop system clock, so the same clock-
        # drift rate measured against the webcam's own audio (see
        # refine_webcam_start / main()) is applied to the cast PiP's own
        # timeline too, the same way as the webcam branch.
        captured = {}
        monkeypatch.setattr(
            cv.subprocess, "run", lambda cmd, check=True: captured.update(cmd=cmd)
        )
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1280,
            720,
            cast=str(tmp_path / "cast.mp4"),
            cast_start=5.0,
            cast_rate=0.0005,
        )
        fchain = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        cast_chain = fchain.split("[1:v]")[1].split("[castpip]")[0]
        assert cast_chain.startswith("setpts=PTS/0.9995")
        assert cast_chain.index("setpts=") < cast_chain.index(
            f"fps={video_format.RENDER_FPS}"
        )
        assert 0.0 < cv.CAST_PIP_ALPHA < 1.0  # actually transparent, not opaque


class TestWebcamSync:
    def test_parse_webcam_wall_reads_filename_timestamp(self):
        assert parse_webcam_wall("VID_20260706_180003.mp4") == datetime(
            2026, 7, 6, 18, 0, 3
        )

    def test_sync_derives_the_cams_own_offset_not_the_recorders(self):
        # Main WAV recorder's own convention: wall = UTC+2 (mirrors
        # TestTimeline.test_derive_utc_offset).
        segs = [
            Segment("a", datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
        ]
        qsos = [
            Qso(
                datetime(2026, 7, 4, 9, 0),
                "A",
                "599",
                "1",
                "599",
                "2",
                "JN97MM",
                10,
                False,
            ),
            Qso(
                datetime(2026, 7, 4, 9, 2),
                "B",
                "599",
                "3",
                "599",
                "4",
                "JN97MM",
                10,
                False,
            ),
        ]
        offset_h = derive_utc_offset(segs, qsos)
        assert offset_h == 2

        # The phone uses a *different* clock convention (UTC+5, not +2) --
        # its filename wall-clock is 14:00:00, real recording start is UTC
        # 09:00:00, which is exactly the start of the session.
        cam_wall = datetime(2026, 7, 4, 14, 0, 0)
        start = sync_webcam_start(
            cam_wall, cam_dur=120.0, qsos=qsos, segs=segs, offset_h=offset_h
        )
        assert start == 0.0

    def test_sync_clamps_to_session_start_when_cam_starts_earlier(self):
        segs = [Segment("a", datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0)]
        qsos = [
            Qso(
                datetime(2026, 7, 4, 9, 0),
                "A",
                "599",
                "1",
                "599",
                "2",
                "JN97MM",
                10,
                False,
            )
        ]
        # cam wall-clock a full day earlier than the session -- however its
        # own offset resolves, the real recording predates segs[0], so the
        # result clamps to the session's own start rather than going negative.
        cam_wall = datetime(2026, 7, 3, 8, 0, 0)
        start = sync_webcam_start(
            cam_wall, cam_dur=30.0, qsos=qsos, segs=segs, offset_h=2
        )
        assert start == 0.0


def _burst_signal(
    sr: int,
    total_dur: float,
    burst_starts: float | list[float],
    burst_dur: float = 0.15,
    amp: float = 1000.0,
    seed: int = 0,
) -> np.ndarray:
    """A mostly-silent signal with noise bursts at `burst_starts` -- a
    stand-in for a short spoken utterance's amplitude envelope (several
    bursts approximate the rhythm of syllables), for testing the webcam
    audio drift-correction cross-correlation without needing a real
    recording. Two signals built with the same seed and burst pattern have
    identical envelope shape, so cross-correlating them finds an exact,
    unambiguous match at whatever offset they were placed -- a single
    burst is deliberately *not* used for anything beyond the most basic
    envelope check, since one isolated spike against silence resembles any
    other isolated spike regardless of content, unlike a multi-burst
    rhythm pattern (see test_find_offset_correction_low_confidence_on_
    unrelated_audio, which relies on this to tell genuinely different
    speech rhythms apart)."""
    if isinstance(burst_starts, (int, float)):
        burst_starts = [burst_starts]
    n = int(total_dur * sr)
    x = np.zeros(n)
    rng = np.random.default_rng(seed)
    for b in burst_starts:
        i0 = int(b * sr)
        i1 = min(n, i0 + int(burst_dur * sr))
        if i0 < n:
            x[i0:i1] = rng.normal(0, amp, i1 - i0)
    return x


def _silence_signal(sr: int, dur: float) -> np.ndarray:
    """Pure silence -- a stand-in for a webcam window with no matching
    speech at all (e.g. corresponding to an RX segment, or a stretch where
    the operator wasn't talking), for testing that _find_offset_correction
    reports zero confidence rather than latching onto a spurious partial
    match. Deliberately not random noise: noise has a nonzero chance of
    producing an accidental partial correlation peak against a bursty
    (speech-like) signal purely by chance, especially when searching many
    candidate offsets -- flaky in a way pure silence (zero variance, so
    the normalized correlation's denominator is exactly zero) cannot be."""
    return np.zeros(int(dur * sr))


class TestWebcamDriftCorrection:
    """The phone and the radio recorder are independent devices whose
    clocks don't tick at exactly the same rate -- refine_webcam_start finds
    this from audio cross-correlation against the operator's own TX audio
    (see its docstring for the real case this was found from: a webcam PiP
    that looked correctly synced at the start of a session but was several
    seconds off by the end, confirmed by ear to be the same words reaching
    the phone's own mic and the radio mic at different points on the
    output timeline)."""

    def test_rms_envelope_captures_a_burst(self):
        sr = 1000
        x = _burst_signal(sr, 2.0, burst_starts=1.0, burst_dur=0.2, amp=500.0)
        env = _rms_envelope(x, sr, win_s=0.05)
        loud_idx = int(np.argmax(env))
        # burst spans [1.0, 1.2)s -> windows [20, 24) at 0.05s/window
        assert 20 <= loud_idx < 24

    def test_find_offset_correction_recovers_a_known_shift(self):
        sr = 16000
        padding_s = 5.0
        radio_bursts = [1.0, 1.4, 1.9, 2.3]  # a rhythm, like a few syllables
        true_correction = 2.0
        radio = _burst_signal(sr, 3.0, radio_bursts, seed=1)
        cam_bursts = [padding_s - true_correction + b for b in radio_bursts]
        cam = _burst_signal(sr, 3.0 + 2 * padding_s, cam_bursts, seed=1)
        correction, confidence = _find_offset_correction(radio, sr, cam, sr, padding_s)
        assert abs(correction - true_correction) < 0.1
        assert confidence > 0.3

    def test_find_offset_correction_low_confidence_on_unrelated_audio(self):
        sr = 16000
        padding_s = 5.0
        radio = _burst_signal(sr, 3.0, [1.0, 1.4, 1.9, 2.3], seed=1)
        # no matching speech at all -- e.g. the webcam window for an RX
        # segment, where the operator wasn't talking
        cam = _silence_signal(sr, 3.0 + 2 * padding_s)
        _, confidence = _find_offset_correction(radio, sr, cam, sr, padding_s)
        assert confidence == 0.0

    def test_refine_webcam_start_fits_linear_drift(self, monkeypatch):
        # Regression test built directly from a real case: sampling
        # confident anchors across a ~2-hour session found the needed
        # correction growing smoothly from ~0s near the start to ~+3.2s
        # near the end -- a linear drift a single constant offset cannot
        # express. This synthesizes that same shape (known intercept and
        # rate) with synthetic audio, so the test is deterministic.
        sr = 16000
        webcam_start_coarse = 100.0
        padding_s = 8.0
        radio_dur = 3.0
        radio_bursts = [1.0, 1.4, 1.9, 2.3]
        true_intercept = 2.0
        true_rate = 0.0005

        audio_ts = [100.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
        segs = [
            Segment(
                f"seg{i}.wav", datetime(2026, 7, 4, 13, 0, 0), radio_dur, t, ptt=True
            )
            for i, t in enumerate(audio_ts)
        ]

        def fake_read_wav_range(path, t0, t1):
            return _burst_signal(sr, radio_dur, radio_bursts, seed=1), sr

        def fake_read_webcam_audio_range(webcam_path, src_start, dur, sr=16000):
            seg_audio_t = src_start + webcam_start_coarse + padding_s
            true_correction = true_intercept + true_rate * seg_audio_t
            cam_bursts = [padding_s - true_correction + b for b in radio_bursts]
            return _burst_signal(sr, dur, cam_bursts, seed=1), sr

        monkeypatch.setattr(webcam_sync, "read_wav_range", fake_read_wav_range)
        monkeypatch.setattr(
            webcam_sync, "_read_webcam_audio_range", fake_read_webcam_audio_range
        )

        refined, rate, n = refine_webcam_start(
            "fake_cam.mp4",
            segs,
            webcam_start_coarse,
            max_anchors=20,
            padding_s=padding_s,
        )
        assert n == len(segs)
        assert abs(rate - true_rate) < 0.0001
        assert abs((refined - webcam_start_coarse) - true_intercept) < 0.2

    def test_refine_webcam_start_unchanged_with_no_confident_anchors(self, monkeypatch):
        segs = [Segment("a.wav", datetime(2026, 7, 4, 13, 0, 0), 3.0, 100.0, ptt=True)]

        def fake_read_wav_range(path, t0, t1):
            return _burst_signal(16000, 3.0, [1.0, 1.4, 1.9, 2.3], seed=1), 16000

        def fake_read_webcam_audio_range(webcam_path, src_start, dur, sr=16000):
            return _silence_signal(sr, dur), sr  # no matching speech at all

        monkeypatch.setattr(webcam_sync, "read_wav_range", fake_read_wav_range)
        monkeypatch.setattr(
            webcam_sync, "_read_webcam_audio_range", fake_read_webcam_audio_range
        )

        refined, rate, n = refine_webcam_start("fake_cam.mp4", segs, 100.0)
        assert (refined, rate, n) == (100.0, 0.0, 0)


class TestWebcamStartWall:
    def test_reads_first_webcam_start_event(self, tmp_path):
        # An Alt+V logger-recorded webcam logs its exact same-machine start
        # into the shared *-input.jsonl, alongside text/qso events.
        f = tmp_path / "input.jsonl"
        f.write_text(
            '{"t": "2026-07-14T18:20:54.000000Z", "event": "text", "text": "H"}\n'
            '{"t": "2026-07-14T18:21:03.836107Z", "event": "webcam_start"}\n'
            '{"t": "2026-07-14T18:23:59.413453Z", "event": "webcam_stop"}\n'
        )
        assert webcam_start_wall(str(f)) == datetime(2026, 7, 14, 18, 21, 3, 836107)

    def test_none_when_no_webcam_start_event(self, tmp_path):
        # Input log from before the Alt+V webcam feature -- caller falls back
        # to the phone filename-timestamp path (parse_webcam_wall).
        f = tmp_path / "input.jsonl"
        f.write_text(
            '{"t": "2026-07-14T18:20:54.000000Z", "event": "text", "text": "H"}\n'
            '{"t": "2026-07-14T18:21:05.000000Z", "event": "qso", "call": "HA5MIG"}\n'
        )
        assert webcam_start_wall(str(f)) is None


class TestWebcamStartFromLog:
    # Real magnitudes from an actual capture: a v4l2 monotonic start is
    # ~1.7e6 (uptime seconds); a wallclock epoch is ~1.78e9.
    _V4L2_HDR = "Input #0, video4linux2,v4l2, from '/dev/video0':\n"
    _PULSE_HDR = "Input #1, pulse, from 'default':\n"

    def test_prefers_v4l2_wallclock_when_flag_present(self, tmp_path):
        # With -use_wallclock_as_timestamps 1 the video input's start: is a
        # true epoch -- the exact frame-0 wallclock, preferred over audio.
        f = tmp_path / "cam.log"
        f.write_text(
            self._V4L2_HDR
            + "  Duration: N/A, start: 1784053264.000000, bitrate: 147456 kb/s\n"
            + self._PULSE_HDR
            + "  Duration: N/A, start: 1784053265.500000, bitrate: 1536 kb/s\n"
        )
        assert webcam_start_from_log(str(f)) == datetime(2026, 7, 14, 18, 21, 4)

    def test_falls_back_to_pulse_when_v4l2_is_monotonic(self, tmp_path):
        # An older recording without the flag: v4l2 start is CLOCK_MONOTONIC
        # (uptime, < 1e9) and unusable, but pulse always reports a wallclock
        # epoch -- still far more precise than the ~1s-early logged event.
        f = tmp_path / "cam.log"
        f.write_text(
            self._V4L2_HDR
            + "  Duration: N/A, start: 1765606.323676, bitrate: 147456 kb/s\n"
            + self._PULSE_HDR
            + "  Duration: N/A, start: 1784053264.967854, bitrate: 1536 kb/s\n"
        )
        assert webcam_start_from_log(str(f)) == datetime(2026, 7, 14, 18, 21, 4, 967854)

    def test_none_when_no_absolute_epoch(self, tmp_path):
        f = tmp_path / "cam.log"
        f.write_text(
            self._V4L2_HDR
            + "  Duration: N/A, start: 1765606.323676, bitrate: 147456 kb/s\n"
        )
        assert webcam_start_from_log(str(f)) is None

    def test_none_when_log_missing(self, tmp_path):
        assert webcam_start_from_log(str(tmp_path / "nope.log")) is None


class TestParseWebcamPreciseFilename:
    def test_parses_the_timestamp_puskas_logger_bakes_in_on_stop(self):
        assert parse_webcam_precise_filename(
            "260706-HA5LA-webcam-20260706T160037.123456Z.mp4"
        ) == datetime(2026, 7, 6, 16, 0, 37, 123456)

    def test_works_with_a_full_path(self):
        assert parse_webcam_precise_filename(
            "/home/op/contest/260706-HA5LA-webcam-20260706T160037.123456Z.mp4"
        ) == datetime(2026, 7, 6, 16, 0, 37, 123456)

    def test_none_for_a_plain_webcam_mp4_not_yet_renamed(self):
        assert parse_webcam_precise_filename("260706-HA5LA-webcam.mp4") is None

    def test_none_for_the_coarse_phone_clip_convention(self):
        assert parse_webcam_precise_filename("VID_20260706_180003.mp4") is None
