"""Tests for contest_video pure logic and the CW decoder.

No ffmpeg is invoked; the decoder is exercised against a synthesized CW WAV so
the test is fully reproducible (fixed WPM, pitch, sample rate)."""

import json
import re
import struct
import wave
from datetime import datetime, timezone

import numpy as np
import pyte
from PIL import Image, ImageDraw, ImageFont

import contest_video as cv
from contest_video import (
    CAPTION_DUR_S,
    CAST_BG,
    GAP_KEEP_S,
    MAX_OVER_S,
    SCOPE_AMP_MAX,
    CharEvent,
    InputLogEvent,
    Qso,
    Segment,
    SegState,
    TelemetrySample,
    _cast_color,
    _CastScreen,
    _CastStream,
    _dominance,
    _draw_cast_row,
    _eff,
    _find_offset_correction,
    _quality,
    _resize_scope_row,
    _rms_envelope,
    _scope_colormap,
    _srt_time,
    _yt_time,
    audio_time_for,
    build_chapters,
    build_srt,
    build_state_events,
    cluster_starts,
    cw_subranges,
    decode_long_segment,
    decode_segment,
    derive_utc_offset,
    gate_events,
    load_input_log,
    load_telemetry,
    match_qso_times,
    merge_edi,
    parse_cast_header,
    parse_edi,
    parse_wav_title,
    parse_webcam_precise_filename,
    parse_webcam_wall,
    qso_windows,
    read_wav_metadata,
    refine_webcam_start,
    remap_audio_t,
    render_scope_video,
    sync_webcam_start,
    trim_to_duration,
    webcam_start_from_log,
    webcam_start_wall,
)
from icom_net import write_scope_record

SR = 16000
PITCH = 600.0

_MORSE_INV = {v: k for k, v in cv.MORSE.items()}


def _write_wav_with_title(path: str, title: str) -> None:
    """A minimal WAV file carrying an IC-9700-style LIST/INFO/INAM title tag,
    for testing read_wav_metadata without needing a real recording."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 100)
    raw = title.encode("ascii") + b"\x00"
    pad = b"\x00" if len(raw) % 2 else b""
    inam = b"INAM" + struct.pack("<I", len(raw)) + raw + pad
    list_data = b"INFO" + inam
    list_pad = b"\x00" if len(list_data) % 2 else b""
    list_chunk = b"LIST" + struct.pack("<I", len(list_data)) + list_data + list_pad
    data = bytearray(open(path, "rb").read())
    data.extend(list_chunk)
    data[4:8] = struct.pack("<I", len(data) - 8)
    with open(path, "wb") as f:
        f.write(data)


def _write_cw(
    path: str, text: str, wpm: int = 24, amp: float = 8000.0, noise: float = 0.0
) -> None:
    """Render `text` as Morse into a 16 kHz mono WAV at `path`."""
    unit = 1.2 / wpm  # seconds per dit
    # standard timing: dit 1u, dah 3u, symbol gap 1u, char gap 3u, word gap 7u
    on: list[tuple[bool, float]] = []
    for wi, word in enumerate(text.split(" ")):
        if wi:
            on.append((False, 7 * unit))  # word gap
        for ci, ch in enumerate(word):
            if ci:
                on.append((False, 3 * unit))  # char gap
            for si, sym in enumerate(_MORSE_INV[ch]):
                if si:
                    on.append((False, unit))  # symbol gap
                on.append((True, unit if sym == "." else 3 * unit))
    on.append((False, 3 * unit))  # trailing silence

    samples: list[np.ndarray] = []
    phase = 0.0
    for is_on, dur in on:
        n = int(dur * SR)
        t = (np.arange(n) + phase) / SR
        phase += n
        tone = np.sin(2 * np.pi * PITCH * t) * (amp if is_on else 0.0)
        samples.append(tone)
    sig = np.concatenate(samples)
    if noise:
        sig = sig + np.random.default_rng(0).normal(0, noise, len(sig))
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(sig.astype(np.int16).tobytes())
    w.close()


def _write_long_wav_with_cw_window(
    path: str,
    total_dur: float,
    cw_start: float,
    text: str,
    wpm: int = 20,
    pitch: float = PITCH,
    amp: float = 8000.0,
) -> tuple[float, float]:
    """Write a `total_dur`-second WAV that's silent except for `text` keyed
    as CW starting at `cw_start` -- simulating a segment far longer than
    MAX_OVER_S (e.g. listening to two other stations for several minutes)
    that still contains one real, decodable CW exchange somewhere inside it.
    Returns the CW window's own (start, end) in seconds."""
    unit = 1.2 / wpm
    on: list[tuple[bool, float]] = []
    for wi, word in enumerate(text.split(" ")):
        if wi:
            on.append((False, 7 * unit))
        for ci, ch in enumerate(word):
            if ci:
                on.append((False, 3 * unit))
            for si, sym in enumerate(_MORSE_INV[ch]):
                if si:
                    on.append((False, unit))
                on.append((True, unit if sym == "." else 3 * unit))
    cw_chunks: list[np.ndarray] = []
    phase = 0.0
    for is_on, dur in on:
        n = int(dur * SR)
        t = (np.arange(n) + phase) / SR
        phase += n
        cw_chunks.append(np.sin(2 * np.pi * pitch * t) * (amp if is_on else 0.0))
    cw = np.concatenate(cw_chunks)

    n_total = int(total_dur * SR)
    sig = np.zeros(n_total)
    i0 = int(cw_start * SR)
    n_fit = min(len(cw), max(0, n_total - i0))
    sig[i0 : i0 + n_fit] = cw[:n_fit]

    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(sig.astype(np.int16).tobytes())
    w.close()
    return cw_start, cw_start + len(cw) / SR


class TestDecoder:
    def test_decodes_clean_callsign_exchange(self, tmp_path):
        p = str(tmp_path / "20260704_120000A.wav")
        _write_cw(p, "HG7F DE HA5LA 5NN TT1 JN97MM", wpm=24)
        events, snr = decode_segment(p, PITCH)
        text = "".join(e.ch for e in events)
        assert text.replace(" ", "") == "HG7FDEHA5LA5NNTT1JN97MM"
        assert snr > 20

    def test_character_timestamps_increase(self, tmp_path):
        p = str(tmp_path / "20260704_120000A.wav")
        _write_cw(p, "CQ TEST", wpm=20)
        events, _ = decode_segment(p, PITCH)
        times = [e.t for e in events]
        assert times == sorted(times)
        assert times[0] >= 0.0

    def test_silence_yields_no_events(self, tmp_path):
        p = str(tmp_path / "20260704_120000A.wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.zeros(SR * 3, np.int16).tobytes())
        w.close()
        events, _ = decode_segment(p, PITCH)
        assert events == []

    def test_decodes_across_a_range_of_wpm(self, tmp_path):
        # dit length is estimated fresh per segment (never a fixed WPM
        # assumption), so different overs at different speeds must each
        # decode correctly on their own.
        text = "CQ TEST DE HA5LA"
        expected = text.replace(" ", "")
        for wpm in (12, 18, 24, 30, 35, 45):
            p = str(tmp_path / f"20260704_120000A_{wpm}.wav")
            _write_cw(p, text, wpm=wpm)
            events, _ = decode_segment(p, PITCH)
            decoded = "".join(e.ch for e in events).replace(" ", "")
            assert decoded == expected, f"wpm={wpm}: got {decoded!r}"


class TestDecoderRobustness:
    @staticmethod
    def _cw_tone(text, wpm, pitch, amp, phase0=0.0):
        unit = 1.2 / wpm
        on: list[tuple[bool, float]] = []
        for wi, word in enumerate(text.split(" ")):
            if wi:
                on.append((False, 7 * unit))
            for ci, ch in enumerate(word):
                if ci:
                    on.append((False, 3 * unit))
                for si, sym in enumerate(_MORSE_INV[ch]):
                    if si:
                        on.append((False, unit))
                    on.append((True, unit if sym == "." else 3 * unit))
        on.append((False, 3 * unit))
        samples: list[np.ndarray] = []
        phase = phase0
        for is_on, dur in on:
            n = int(dur * SR)
            t = (np.arange(n) + phase) / SR
            phase += n
            samples.append(np.sin(2 * np.pi * pitch * t) * (amp if is_on else 0.0))
        return np.concatenate(samples)

    def test_moderate_offset_interference_snr_improves(self, tmp_path):
        # Regression test verified against real recordings: a same-band CW-like
        # interferer ~150 Hz away partially leaks through the old envelope
        # filter's wide, poorly-shaped passband, depressing the measured SNR.
        # A properly windowed lowpass rejects it noticeably better at this
        # distance (measured baseline on unmodified code: 14.65 dB). Interference
        # much closer than this (< ~100 Hz) genuinely overlaps the wanted
        # signal's own keying spectrum and cannot be separated by filtering
        # alone -- this test only covers the distance where filtering helps.
        wanted = self._cw_tone("HG7F DE HA5LA 5NN TT1 JN97MM", 24, PITCH, 8000.0)
        interf = self._cw_tone(
            "CQ CQ DE HG1Z HG1Z TEST CQ CQ DE HG1Z TEST",
            28,
            PITCH + 150,
            6000.0,
            phase0=137,
        )
        sig = wanted + np.resize(interf, len(wanted))
        p = str(tmp_path / "20260704_120000A.wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(sig.astype(np.int16).tobytes())
        w.close()
        _, snr = decode_segment(p, PITCH)
        assert snr > 16.0

    def test_decodes_correctly_when_actual_tone_is_far_from_assumed_pitch(
        self, tmp_path
    ):
        # Regression test for a real reported bug, much more severe than the
        # small WAV/telemetry frequency disagreement found earlier: a real
        # received-signal segment's actual tone was ~1296 Hz against the
        # assumed 600 Hz -- a 695 Hz gap entirely outside the envelope
        # lowpass's passband (LOWPASS_CUTOFF_HZ=120), so almost none of the
        # real signal survived demodulation at the wrong frequency at all.
        # decode_segment must auto-detect the real tone per segment rather
        # than trusting a single assumed pitch for the whole session.
        tone = self._cw_tone("HG7F DE HA5LA", 20, 1300.0, 8000.0)
        p = str(tmp_path / "20260704_120000A.wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(tone.astype(np.int16).tobytes())
        w.close()
        events, _ = decode_segment(p, 600.0)  # deliberately wrong nominal pitch
        assert "".join(e.ch for e in events).strip() == "HG7F DE HA5LA"

    @staticmethod
    def _cw_tone_with_dah_glitches(text, wpm, pitch, amp, glitch_frac=0.3):
        """Like _cw_tone, but splits every dah with a brief spurious dropout
        in the middle -- simulating the near-threshold chatter a real
        received signal has that the operator's own clean TX sidetone
        doesn't (see DEBOUNCE_DIT_FRAC)."""
        unit = 1.2 / wpm
        on: list[tuple[bool, float]] = []
        for wi, word in enumerate(text.split(" ")):
            if wi:
                on.append((False, 7 * unit))
            for ci, ch in enumerate(word):
                if ci:
                    on.append((False, 3 * unit))
                for si, sym in enumerate(_MORSE_INV[ch]):
                    if si:
                        on.append((False, unit))
                    dur = unit if sym == "." else 3 * unit
                    if sym == "-":
                        g = unit * glitch_frac
                        on.append((True, dur / 2 - g / 2))
                        on.append((False, g))
                        on.append((True, dur / 2 - g / 2))
                    else:
                        on.append((True, dur))
        on.append((False, 3 * unit))
        samples: list[np.ndarray] = []
        phase = 0.0
        for is_on, dur in on:
            n = int(dur * SR)
            t = (np.arange(n) + phase) / SR
            phase += n
            samples.append(np.sin(2 * np.pi * pitch * t) * (amp if is_on else 0.0))
        return np.concatenate(samples)

    def test_debounce_recovers_text_fragmented_by_near_threshold_chatter(
        self, tmp_path
    ):
        # Regression test for a real reported bug: a received-signal segment
        # with known ground truth (the user transcribed it by ear) decoded
        # to gibberish despite a high (33 dB) SNR. Root cause found by
        # dumping the raw hysteresis run durations: many on/off runs were a
        # fraction of a dit long, fragmenting single dits/dahs into several
        # pieces -- the operator's own TX sidetone is clean and never does
        # this, but a real received signal's near-threshold noise does.
        # This synthesizes the same failure mode (a brief dropout injected
        # into the middle of every dah) on a fully clean signal otherwise,
        # so the test is deterministic and needs no real recording.
        # Verified red before green: monkeypatching _debounce_on back to a
        # no-op on this exact signal decodes to 'H55 HE HS55S 5SS II SHH'.
        text = "HG7F DE HA5LA 5NN TT1 JN97MM"
        sig = self._cw_tone_with_dah_glitches(text, 20, PITCH, 8000.0, glitch_frac=0.3)
        p = str(tmp_path / "20260704_120000A.wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(sig.astype(np.int16).tobytes())
        w.close()
        events, _ = decode_segment(p, PITCH)
        assert "".join(e.ch for e in events).strip() == text

    def test_long_segment_is_skipped_without_decoding(self, tmp_path):
        # Segments longer than MAX_OVER_S always fail gate_events on duration
        # alone, so decode_segment should short-circuit rather than run the
        # full envelope/threshold pipeline over what can be minutes of audio.
        p = str(tmp_path / "20260704_120000A.wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        n = int((cv.MAX_OVER_S + 1) * SR)
        rng = np.random.default_rng(0)
        w.writeframes((rng.normal(0, 3000, n)).astype(np.int16).tobytes())
        w.close()
        events, snr = decode_segment(p, PITCH)
        assert events == []
        assert snr == 0.0


class TestGate:
    def test_quality_rewards_multichar_tokens(self):
        assert _quality("HG7F DE HA5LA") == 1.0
        assert _quality("E T I S E") == 0.0
        assert _quality("") == 0.0

    def test_dominance_flags_chopped_carrier(self):
        assert _dominance("TTTTTTTT") == 1.0
        assert _dominance("HG7F DE HA5LA") < 0.4

    def test_real_over_passes_gate(self):
        ev = [CharEvent(0.1 * i, c) for i, c in enumerate("HA5LA DE HG7F")]
        assert gate_events(15.0, ev, snr=40.0) == ev

    def test_long_noisy_segment_rejected(self):
        ev = [CharEvent(0.1 * i, c) for i, c in enumerate("E T E T I E S")]
        assert gate_events(474.0, ev, snr=25.0) == []  # too long
        assert gate_events(10.0, ev, snr=25.0) == []  # low quality

    def test_chopped_carrier_rejected(self):
        ev = [CharEvent(0.1 * i, "T") for i in range(40)]
        assert gate_events(12.0, ev, snr=30.0) == []

    def test_short_valid_words_are_not_falsely_dominance_rejected(self):
        # Regression test for a real reported bug: a correctly-decoded "TU"
        # and "73 EE" were silently dropped from the ticker. Any 2-character
        # decode has dominance >= 0.5 by construction -- the two characters
        # either match (1.0) or don't (exactly 1/2), never less -- so
        # MAX_DOMINANCE=0.4 was structurally impossible to pass for *any*
        # two-letter contest word ("TU", "R", "K"...), independent of content.
        assert _dominance("TU") == 0.0
        assert _dominance("73EE") == 0.0
        # the pattern this check actually guards against still gets caught
        # once there's enough text for "chopped carrier" to show at all
        assert _dominance("TTTTTTTT") == 1.0


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
        assert f"fps={cv.RENDER_FPS}" in pip_chain

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
        assert pip_chain.index("setpts=") < pip_chain.index(f"fps={cv.RENDER_FPS}")

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
        assert cast_chain.index("setpts=") < cast_chain.index(f"fps={cv.RENDER_FPS}")
        assert 0.0 < cv.CAST_PIP_ALPHA < 1.0  # actually transparent, not opaque


class TestLongSegmentCwRecovery:
    """decode_long_segment recovers CW content from a segment too long to
    decode as a whole (see MAX_OVER_S) -- e.g. two other stations
    negotiating a CW frequency over voice, working each other in CW, then
    moving on, all while we just listened without ever transmitting
    ourselves, so our own recorder never split the file."""

    def test_gate_events_check_duration_false_bypasses_length_but_not_quality(self):
        ev = [CharEvent(0.1 * i, c) for i, c in enumerate("HA5LA DE HG7F")]
        # a real over this long is normally rejected outright ...
        assert gate_events(474.0, ev, snr=40.0) == []
        # ... but not once duration is confirmed genuine by other means
        # (telemetry mode confirmation, for a sub-range extracted from a
        # longer segment)
        assert gate_events(474.0, ev, snr=40.0, check_duration=False) == ev
        # SNR/quality/dominance still apply regardless
        noisy = [CharEvent(0.1 * i, "T") for i in range(40)]
        assert gate_events(474.0, noisy, snr=40.0, check_duration=False) == []

    def test_cw_subranges_extracts_only_cw_windows_within_segment_span(self):
        seg = Segment("a", datetime(2026, 7, 6, 16, 30, 45), 300.0, 1000.0)
        state_events = [
            (900.0, 1010.0, SegState(mode="FM")),  # starts before seg -- FM, ignored
            (1010.0, 1080.0, SegState(mode="CW")),  # fully inside -- CW, kept
            (1080.0, 1200.0, SegState(mode="SSB")),  # inside -- SSB, ignored
            (1200.0, 1400.0, SegState(mode="CW")),  # ends after seg -- CW, clipped
        ]
        # seg spans [1000, 1300); results are relative to seg.audio_t (1000)
        assert cw_subranges(seg, state_events) == [(10.0, 80.0), (200.0, 300.0)]

    def test_decode_long_segment_recovers_cw_from_a_too_long_segment(self, tmp_path):
        # Regression test for a real reported case: two other stations
        # negotiate a CW frequency over voice, work each other in CW, then
        # move on -- all while we just listened, so our own recorder never
        # split the file and the whole thing became one segment far longer
        # than MAX_OVER_S. decode_segment alone never even attempts to
        # decode any of it; decode_long_segment recovers the CW portion
        # using telemetry's own confirmation of exactly when our radio was
        # tuned to their frequency in CW mode.
        p = str(tmp_path / "20260706_163045A.wav")
        total_dur = MAX_OVER_S * 3
        text = "HG7F DE HA5LA"
        cw_start = MAX_OVER_S * 1.2
        _, cw_end = _write_long_wav_with_cw_window(p, total_dur, cw_start, text)
        seg = Segment(p, datetime(2026, 7, 6, 16, 30, 45), total_dur, 0.0)
        assert seg.dur > MAX_OVER_S

        # whole-file decode never even attempts it
        events, snr = decode_segment(p, PITCH)
        assert events == [] and snr == 0.0

        state_events = [(cw_start, cw_end, SegState(mode="CW"))]
        spans = decode_long_segment(seg, state_events, PITCH)
        assert len(spans) == 1
        t0, t1, events = spans[0]
        assert abs(t0 - cw_start) < 0.01
        assert "".join(e.ch for e in events).strip() == text

    def test_decode_long_segment_ignores_non_cw_subranges(self, tmp_path):
        p = str(tmp_path / "20260706_163045A.wav")
        total_dur = MAX_OVER_S * 3
        _write_long_wav_with_cw_window(p, total_dur, MAX_OVER_S * 1.2, "HG7F")
        seg = Segment(p, datetime(2026, 7, 6, 16, 30, 45), total_dur, 0.0)
        # same audio, but telemetry says this whole span was SSB, not CW --
        # nothing should be extracted or decoded
        state_events = [(0.0, total_dur, SegState(mode="SSB"))]
        assert decode_long_segment(seg, state_events, PITCH) == []

    def test_remap_audio_t_preserves_a_long_segment_with_recovered_cw(self):
        # Without the exemption, --skip-gaps' outpoint trimming in
        # concat_audio would cut the very audio decode_long_segment just
        # recovered text from out of the rendered output entirely.
        long_seg = Segment(
            "long.wav", datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 50, 0.0
        )
        remap_audio_t([long_seg], long_cw_segs={id(long_seg)})
        assert long_seg.eff_dur is None

        other = Segment(
            "other.wav", datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 50, 0.0
        )
        remap_audio_t([other])
        assert other.eff_dur == GAP_KEEP_S


class TestEdi:
    def test_parse_edi(self, tmp_path):
        edi = tmp_path / "log.edi"
        edi.write_text(
            "[REG1TEST;1]\n"
            "PCall=HA5LA\n"
            "PWWLo=JN97MM\n"
            "[QSORecords;2]\n"
            "260704;0908;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
            "260704;0929;HA7NK;2;599;004;599;029;;JN97WW;0;;;D;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        assert (mycall, mywwl) == ("HA5LA", "JN97MM")
        assert len(qsos) == 2
        assert qsos[0].call == "HG7F" and qsos[0].pts == 26
        assert qsos[0].dt == datetime(2026, 7, 4, 9, 8)
        assert qsos[1].dup is True and qsos[1].pts == 0

    def test_merge_edi_combines_and_sorts_multiple_bands(self, tmp_path):
        # A session worked on two bands writes two EDI files -- one physical
        # recording still needs a single chronological QSO list.
        band_2m = tmp_path / "2m.edi"
        band_2m.write_text(
            "PCall=HA5LA\nPWWLo=JN97TF\n[QSORecords;2]\n"
            "260706;1601;A;1;59;001;59;001;;JN86SR;167;;;;\n"
            "260706;1720;C;1;59;003;59;003;;JN86SR;167;;;;\n"
        )
        band_70cm = tmp_path / "70cm.edi"
        band_70cm.write_text(
            "PCall=HA5LA\nPWWLo=JN97TF\n[QSORecords;1]\n"
            "260706;1615;B;1;59;001;59;002;;JN97WM;37;;;;\n"
        )
        mycall, mywwl, qsos = merge_edi([str(band_2m), str(band_70cm)])
        assert (mycall, mywwl) == ("HA5LA", "JN97TF")
        assert [q.call for q in qsos] == [
            "A",
            "B",
            "C",
        ]  # chronological, bands interleaved


class TestTrimToDuration:
    def _segs(self):
        return [
            Segment("a", datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
            Segment("c", datetime(2026, 7, 4, 11, 2, 0), 60.0, 120.0),
        ]

    def test_drops_segments_past_the_cutoff(self):
        out = trim_to_duration(self._segs(), 90.0)
        assert [s.path for s in out] == ["a", "b"]

    def test_shortens_the_last_kept_segment_to_land_on_the_cutoff(self):
        out = trim_to_duration(self._segs(), 90.0)
        assert out[-1].eff_dur == 30.0
        assert _eff(out[-1]) == 30.0

    def test_cutoff_beyond_total_keeps_everything_unchanged(self):
        segs = self._segs()
        out = trim_to_duration(segs, 999.0)
        assert len(out) == 3
        assert out[-1].eff_dur is None


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

        monkeypatch.setattr(cv, "_read_wav_range", fake_read_wav_range)
        monkeypatch.setattr(
            cv, "_read_webcam_audio_range", fake_read_webcam_audio_range
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

        monkeypatch.setattr(cv, "_read_wav_range", fake_read_wav_range)
        monkeypatch.setattr(
            cv, "_read_webcam_audio_range", fake_read_webcam_audio_range
        )

        refined, rate, n = refine_webcam_start("fake_cam.mp4", segs, 100.0)
        assert (refined, rate, n) == (100.0, 0.0, 0)


def _write_cast(path, width, height, ts, events):
    """A minimal asciinema cast v2 file for testing, without needing a
    real `asciinema rec` session. `events` is [(t, text)] output events."""
    with open(path, "w") as f:
        f.write(
            json.dumps(
                {"version": 2, "width": width, "height": height, "timestamp": ts}
            )
            + "\n"
        )
        for t, text in events:
            f.write(json.dumps([t, "o", text]) + "\n")


class TestTerminalCast:
    """Rendering an asciinema .cast (e.g. an irssi+logger tmux session) as
    a video PIP -- see render_cast_video's docstring for why this is text
    rasterized via pyte+PIL rather than a GIF/agg conversion, and why sync
    needs no cross-correlation at all (the cast's own header embeds an
    exact Unix-epoch start time, from the same machine's clock)."""

    def test_parse_cast_header_reads_exact_utc_start(self, tmp_path):
        p = tmp_path / "session.cast"
        # 1783890785 is 2026-07-12 21:13:05 UTC (real value from a real
        # asciinema recording, cross-checked against `date -d @1783890785`)
        _write_cast(str(p), 191, 52, 1783890785, [])
        start, w, h = parse_cast_header(str(p))
        assert start == datetime(2026, 7, 12, 21, 13, 5)
        assert (w, h) == (191, 52)

    def test_cast_color_falls_back_to_default_for_unknown_name(self):
        default = (1, 2, 3)
        assert _cast_color(None, default) == default
        assert _cast_color("default", default) == default
        assert _cast_color("nonexistent-color-name", default) == default

    def test_cast_color_looks_up_known_names(self):
        assert _cast_color("red", (0, 0, 0)) == cv.CAST_PALETTE["red"]

    def test_draw_cast_row_descender_survives_the_row_belows_own_redraw(self):
        # Regression test for a real bug: a naive 1.2x-of-font-size line
        # height (15px at CAST_FONT_SIZE=13) undershot DejaVu Sans Mono's
        # real metrics (ascent+descent=17px). _draw_cast_row erases and
        # redraws exactly one row's own rectangle at a time (see its
        # docstring), so a descender glyph like '_' that spilled past a
        # too-short row height got clipped the next time the row *below*
        # was independently redrawn -- even though the row with the
        # underscore never changed. Found from a real rendered frame: a
        # static irssi banner's underscores were visibly missing partway
        # through a render.  Verified red before green: this exact
        # comparison showed 39 differing pixels with the old int(size*1.2)
        # formula and 0 with font.getmetrics()-based line height.
        font = ImageFont.truetype(cv.CAST_FONT_PATH, cv.CAST_FONT_SIZE)
        font_b = ImageFont.truetype(cv.CAST_FONT_BOLD, cv.CAST_FONT_SIZE)
        cw = font.getlength("M")
        ascent, descent = font.getmetrics()
        lh = ascent + descent
        W, H = 5, 2

        screen = pyte.Screen(W, H)
        stream = pyte.ByteStream(screen)
        stream.feed(b"_____")

        px_w, px_h = int(cw * W) + 4, lh * H + 4
        crop_h = min(px_h, lh + 5)  # a margin below row 0's own rectangle

        canvas_alone = cv.Image.new("RGB", (px_w, px_h), CAST_BG)
        _draw_cast_row(
            ImageDraw.Draw(canvas_alone), screen.buffer[0], 0, W, font, font_b, cw, lh
        )
        row0_alone = np.array(canvas_alone.crop((0, 0, px_w, crop_h)))

        canvas_after = cv.Image.new("RGB", (px_w, px_h), CAST_BG)
        draw_after = ImageDraw.Draw(canvas_after)
        _draw_cast_row(draw_after, screen.buffer[0], 0, W, font, font_b, cw, lh)
        _draw_cast_row(draw_after, screen.buffer[1], 1, W, font, font_b, cw, lh)
        row0_after_row1_redraw = np.array(canvas_after.crop((0, 0, px_w, crop_h)))

        assert np.array_equal(row0_alone, row0_after_row1_redraw)

    # tmux (the logger is recorded running inside it) scrolls/clears a single
    # pane with left/right margins (DECSLRM, CSI Pl;Pr s) + scroll-up (SU,
    # CSI Ps S) -- three sequences stock pyte drops, so a pane never cleared
    # and old content showed through the new (the reported "startup screen
    # still visible behind the contest screen" garbage). _CastScreen/
    # _CastStream implement them; `asciinema play` always did, which is why
    # the cast looked clean there but not in the render.
    _CLEAR = b"\x1b[1;4r\x1b[11;20s\x1b[4S\x1b[1;4r"  # tmux clears cols 11-20

    def test_stock_pyte_leaves_stale_pane_content(self):
        # The bug this fixes: with plain pyte the pane-scroll is a no-op, so
        # the old tail survives -- exactly the garbage seen in the render.
        screen = pyte.Screen(20, 4)
        stream = pyte.ByteStream(screen)
        stream.feed(b"\x1b[1;1H0123456789ABCDEFGHIJ")
        stream.feed(self._CLEAR)
        assert screen.display[0][10:] == "ABCDEFGHIJ"  # NOT cleared

    def test_cast_screen_clears_only_the_pane_columns(self):
        screen = _CastScreen(20, 4)
        stream = _CastStream(screen)
        stream.feed(b"\x1b[1;1H0123456789ABCDEFGHIJ")
        assert screen.display[0] == "0123456789ABCDEFGHIJ"
        stream.feed(self._CLEAR)
        assert screen.display[0][:10] == "0123456789"  # left pane untouched
        assert screen.display[0][10:] == " " * 10  # right pane cleared

    def test_bare_csi_s_is_save_cursor_not_a_margin(self):
        # `CSI s` with <2 params is SCOSC (save cursor), not DECSLRM -- must
        # not set a left/right margin (which would wrongly constrain scrolls).
        screen = _CastScreen(20, 4)
        _CastStream(screen).feed(b"\x1b[s")
        assert screen.margins_lr is None

    def _build_two_pane_screen(self, cls):
        # 20 cols x 4 rows: row0 is a full-width header (must stay outside
        # the vertical scroll margins); rows1-3 hold distinct left-pane
        # (cols0-9) and right-pane (cols10-19) content, like irssi | logger.
        screen = cls(20, 4)
        stream = pyte.ByteStream(screen) if cls is pyte.Screen else _CastStream(screen)
        stream.feed(b"\x1b[1;1H" + b"H" * 20)
        stream.feed(b"\x1b[2;1H0000000000")
        stream.feed(b"\x1b[3;1H1111111111")
        stream.feed(b"\x1b[4;1H2222222222")
        stream.feed(b"\x1b[2;11HAAAAAAAAAA")
        stream.feed(b"\x1b[3;11HBBBBBBBBBB")
        stream.feed(b"\x1b[4;11HCCCCCCCCCC")
        return screen, stream

    def test_stock_pyte_plain_linefeed_drags_the_other_pane_too(self):
        # The bug this fixes: a plain '\n' hitting the bottom margin uses
        # pyte's stock index(), which swaps whole row *objects*
        # (buffer[y] = buffer[y+1]) -- ignoring margins_lr entirely, unlike
        # the explicit-SU path _scroll already handles. Found from the real
        # cast: irssi filling its pane and auto-scrolling on a plain
        # linefeed dragged the logger's pane (outside DECSLRM) up with it.
        screen, stream = self._build_two_pane_screen(pyte.Screen)
        stream.feed(b"\x1b[2;4r\x1b[1;10s")  # DECSTBM rows1-3, DECSLRM cols0-9
        stream.feed(b"\x1b[4;1H\n")  # cursor at bottom margin, plain LF
        # right pane (untouched by DECSLRM) wrongly scrolled too
        assert screen.display[1][10:] == "BBBBBBBBBB"

    def test_cast_screen_plain_linefeed_only_scrolls_its_own_pane(self):
        screen, stream = self._build_two_pane_screen(_CastScreen)
        stream.feed(b"\x1b[2;4r\x1b[1;10s")  # DECSTBM rows1-3, DECSLRM cols0-9
        stream.feed(b"\x1b[4;1H\n")  # cursor at bottom margin, plain LF
        assert screen.display[0] == "H" * 20  # header untouched
        assert screen.display[1] == "1111111111AAAAAAAAAA"  # left scrolled...
        assert screen.display[2] == "2222222222BBBBBBBBBB"  # ...right didn't
        assert screen.display[3] == "          CCCCCCCCCC"


class TestSkipGaps:
    def _segs_with_gap(self):
        # short over (15 s, has events) then long gap (500 s, no events)
        return [
            Segment(
                "a",
                datetime(2026, 7, 4, 11, 0, 0),
                15.0,
                0.0,
                events=[CharEvent(1.0, "H")],
            ),
            Segment("b", datetime(2026, 7, 4, 11, 0, 15), 500.0, 15.0),
        ]

    def test_eff_defaults_to_dur(self):
        s = Segment("x", datetime(2026, 7, 4, 11, 0), 42.0, 0.0)
        assert _eff(s) == 42.0

    def test_remap_shortens_gap_segments(self):
        segs = self._segs_with_gap()
        remap_audio_t(segs)
        assert segs[0].eff_dur is None  # short over: unchanged
        assert segs[1].eff_dur == GAP_KEEP_S  # long gap: trimmed
        assert _eff(segs[0]) == 15.0
        assert _eff(segs[1]) == GAP_KEEP_S

    def test_remap_recomputes_audio_t(self):
        segs = self._segs_with_gap()
        remap_audio_t(segs)
        assert segs[0].audio_t == 0.0
        assert segs[1].audio_t == 15.0  # immediately after the short over

    def test_audio_time_clamps_within_gap(self):
        segs = self._segs_with_gap()
        remap_audio_t(segs)
        # wall time deep inside the gap should map to end of trimmed gap
        deep = datetime(2026, 7, 4, 11, 5, 0)  # 285 s into the gap segment
        t = audio_time_for(deep, segs)
        assert t == 15.0 + GAP_KEEP_S

    def test_total_duration_reduced(self):
        segs = self._segs_with_gap()
        before = segs[-1].audio_t + segs[-1].dur
        remap_audio_t(segs)
        after = segs[-1].audio_t + _eff(segs[-1])
        assert after < before
        assert after == 15.0 + GAP_KEEP_S


class TestTimeline:
    def _segs(self):
        # two 60 s segments, second starts 60 s later in wall time (contiguous)
        return [
            Segment("a", datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
        ]

    def test_audio_time_maps_wall_to_playback(self):
        segs = self._segs()
        assert audio_time_for(datetime(2026, 7, 4, 11, 0, 30), segs) == 30.0
        assert audio_time_for(datetime(2026, 7, 4, 11, 1, 15), segs) == 75.0

    def test_audio_time_clamps_past_end(self):
        segs = self._segs()
        assert audio_time_for(datetime(2026, 7, 4, 12, 0, 0), segs) == 120.0

    def test_derive_utc_offset(self):
        segs = self._segs()  # wall 11:00-11:02 local
        qsos = [
            cv.Qso(
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
            cv.Qso(
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
        assert derive_utc_offset(segs, qsos) == 2


class TestAss:
    def _ticker_texts(self, ass: str) -> list[str]:
        texts = []
        for line in ass.splitlines():
            if line.startswith("Dialogue:") and ",Ticker," in line:
                texts.append(line.rsplit(",", 1)[-1])
        return texts

    def test_cluster_starts_marks_first_segment_and_after_long_gap_only(self):
        segs = [
            Segment(
                "a",
                datetime(2026, 7, 4, 13, 0, 0),
                5.0,
                0.0,
                events=[CharEvent(0.0, "A")],
            ),  # 1st segment: burst start
            Segment(
                "b", datetime(2026, 7, 4, 13, 0, 5), 5.0, 5.0
            ),  # short silence, no events
            Segment(
                "c",
                datetime(2026, 7, 4, 13, 0, 10),
                5.0,
                10.0,
                events=[CharEvent(0.0, "B")],
            ),  # continuation (short gap before it)
            Segment(
                "d", datetime(2026, 7, 4, 13, 0, 15), MAX_OVER_S + 1, 15.0
            ),  # genuine gap
            Segment(
                "e",
                datetime(2026, 7, 4, 13, 0, 50),
                5.0,
                50.0,
                events=[CharEvent(0.0, "C")],
            ),  # new burst
        ]
        assert cluster_starts(segs) == [0.0, 50.0]

    def test_cluster_starts_counts_voice_segments_too(self):
        # Regression test for a real bug found by the user: a WAV segment
        # boundary is a precise real-world RX/TX transition regardless of
        # what's actually being transmitted. A voice-mode QSO's segments
        # never carry decoded CW events (there's no CW there to decode), so
        # requiring `s.events` made cluster_starts blind to every voice
        # over -- on a mostly-voice recording this meant almost no QSO ever
        # got the audio-precise snap at all. Duration alone (a real over is
        # short; a genuine gap is long) works identically for voice and CW.
        segs = [
            Segment(
                "a", datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 1, 0.0
            ),  # listening gap
            Segment(
                "b", datetime(2026, 7, 4, 13, 0, 40), 5.0, 40.0
            ),  # voice over, no CW events
        ]
        assert cluster_starts(segs) == [40.0]

    def test_cluster_starts_skips_leading_rx_to_find_the_tx_start(self):
        # Regression test for the user's own RX/TX heuristic, verified
        # against this exact real burst from the "mix" recording: when a
        # recording/burst begins with the operator listening (RX) rather
        # than transmitting, the burst's own first segment is not where a
        # QSO actually starts -- the QSO starts on the operator's own TX.
        # Without telemetry there's no ground truth, but RX and TX reliably
        # alternate, and TX segments (a brief call/report) are consistently
        # shorter than RX segments (listening for a reply) -- so whichever
        # alternating phase has the shorter median duration is TX, and the
        # first segment in that phase is the real start.
        # (Real durations from urhob2026mix: RX 26.11s, TX 2.13s, RX 5.54s,
        # TX 5.41s -- user confirmed by ear that the TX at t=26.11s is
        # exactly when they started calling.)
        segs = [
            Segment("a", datetime(2026, 7, 4, 13, 0, 0), 26.11, 0.0),  # RX: listening
            Segment(
                "b", datetime(2026, 7, 4, 13, 0, 26), 2.13, 26.11
            ),  # TX: the real start
            Segment(
                "c", datetime(2026, 7, 4, 13, 0, 28), 5.54, 28.24
            ),  # RX: listening for reply
            Segment(
                "d", datetime(2026, 7, 4, 13, 0, 34), 5.41, 33.78
            ),  # TX: continuing
        ]
        assert cluster_starts(segs) == [26.11]

    def test_qso_window_snaps_to_real_burst_not_edi_minute(self, tmp_path):
        # EDI only has minute precision, so audio_time_for(qso.dt) lands
        # somewhere inside the real over rather than at its start. The panel
        # window must snap to where the over actually begins.
        edi = tmp_path / "log.edi"
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1117;HA7NK;2;599;002;599;014;;JN97WW;77;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment(
                "a",
                datetime(2026, 7, 4, 13, 0, 0),
                5.0,
                0.0,
                events=[CharEvent(0.0, "A")],
            ),
            Segment("b", datetime(2026, 7, 4, 13, 0, 5), 474.0, 5.0),
            # real over begins here, well before the EDI's truncated :00 second
            Segment(
                "c",
                datetime(2026, 7, 4, 13, 17, 47),
                5.0,
                479.0,
                events=[CharEvent(0.0, "H")],
            ),
        ]
        offset_h = 2
        total = 484.0
        [(start, _end)] = qso_windows(qsos, segs, offset_h, total)
        assert start == 479.0  # snapped to segment c's real start, not ~486ish

    def test_qso_window_snaps_to_own_burst_not_the_next_ones(self, tmp_path):
        # Regression test for a real bug found by the user: if a QSO takes a
        # while to complete (calling, retries) before being logged, its
        # EDI-derived approximate time can end up numerically *closer* to
        # the following contact's real burst than to its own. Picking the
        # nearest cluster then wrongly snaps QSO N onto QSO N+1's burst. The
        # correct rule is the *latest* burst that started at or before the
        # approximate time, since a QSO's own over must have begun before it
        # was logged.
        edi = tmp_path / "log.edi"
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1301;HA5MA;2;599;003;599;019;;JN97MK;9;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment(
                "a",
                datetime(2026, 7, 4, 13, 0, 0),
                5.0,
                100.0,
                events=[CharEvent(0.0, "X")],
            ),  # this QSO's real burst
            Segment("b", datetime(2026, 7, 4, 13, 0, 5), 100.0, 105.0),  # genuine gap
            Segment(
                "c",
                datetime(2026, 7, 4, 13, 1, 45),
                5.0,
                205.0,
                events=[CharEvent(0.0, "Y")],
            ),  # the *next* contact's burst
        ]
        [(start, _end)] = qso_windows(qsos, segs, offset_h=0, total=210.0)
        assert start == 100.0  # not 205.0 (the next burst, numerically closer)

    def test_qso_window_before_any_cluster_uses_approx_time(self, tmp_path):
        # Regression test for a real bug found by the user on a mostly-voice
        # ("mix" mode) recording: a QSO logged before any CW was ever
        # decoded (e.g. an early SSB contact, or simply the very first QSO)
        # has no earlier cluster to snap to. Falling back to the *first*
        # cluster in the whole recording pulled the panel far into the
        # future (minutes off in the real case) instead of just using the
        # coarse EDI-derived time, which -- while not audio-precise -- is at
        # least in the right neighbourhood.
        edi = tmp_path / "log.edi"
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1300;HA7NK;1;59;001;59;014;;JN97WW;77;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment(
                "a", datetime(2026, 7, 4, 13, 0, 0), 300.0, 0.0
            ),  # voice, no CW events
            Segment(
                "b",
                datetime(2026, 7, 4, 13, 5, 0),
                5.0,
                300.0,
                events=[CharEvent(0.0, "Z")],
            ),  # first-ever CW burst
        ]
        [(start, _end)] = qso_windows(qsos, segs, offset_h=0, total=305.0)
        assert start == 0.0  # not 300.0 (the first cluster, minutes away)


class TestChaptersAndSrt:
    def test_yt_time_formats(self):
        assert _yt_time(0) == "0:00"
        assert _yt_time(65) == "1:05"
        assert _yt_time(3665) == "1:01:05"

    def test_srt_time_formats(self):
        assert _srt_time(65.5) == "00:01:05,500"

    def test_qso_windows_spans_to_next_qso(self, tmp_path):
        edi = tmp_path / "log.edi"
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;2]\n"
            "260704;1100;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
            "260704;1110;HA7NK;2;599;002;599;014;;JN97WW;77;;;;\n"
        )
        _, _, qsos = parse_edi(str(edi))
        segs = [Segment("a", datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0)]
        total = 1200.0
        windows = qso_windows(qsos, segs, offset_h=2, total=total)
        assert len(windows) == 2
        assert windows[0][1] == windows[1][0]  # first ends when second begins
        assert windows[1][1] == total

    def test_build_chapters_starts_at_zero(self, tmp_path):
        edi = tmp_path / "log.edi"
        # QSO 2 min into the segment so its own chapter lands well after 0:00
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1102;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
        )
        _, _, qsos = parse_edi(str(edi))
        segs = [Segment("a", datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0)]
        windows = qso_windows(qsos, segs, offset_h=2, total=1200.0)
        chapters = build_chapters(qsos, windows)
        lines = chapters.strip().splitlines()
        assert lines[0] == "0:00 Start"
        assert "HG7F" in chapters

    def test_build_chapters_includes_band_and_mode(self, tmp_path):
        # PBand header -> band label; per-QSO mode code (1=SSB, 2=CW, 6=FM)
        # -> mode string. Both must appear in the chapter line.
        edi = tmp_path / "log.edi"
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\nPBand=435 MHz\n[QSORecords;1]\n"
            "260704;1102;HG7F;6;59;001;59;010;;JN97KR;26;;;;\n"
        )
        _, _, qsos = parse_edi(str(edi))
        assert (qsos[0].band, qsos[0].mode) == ("70CM", "FM")
        segs = [Segment("a", datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0)]
        windows = qso_windows(qsos, segs, offset_h=2, total=1200.0)
        chapters = build_chapters(qsos, windows)
        line = [ln for ln in chapters.splitlines() if "HG7F" in ln][0]
        assert line.endswith("QSO 001 HG7F  70CM FM")

    def test_build_chapters_drops_qsos_closer_than_min_gap(self):
        qsos = [
            Qso(
                datetime(2026, 7, 4, 11, 0, 0),
                "HG7F",
                "599",
                "001",
                "599",
                "010",
                "JN97KR",
                26,
                False,
            ),
            Qso(
                datetime(2026, 7, 4, 11, 0, 5),
                "HA7NK",
                "599",
                "002",
                "599",
                "014",
                "JN97WW",
                77,
                False,
            ),
        ]
        windows = [(60.0, 65.0), (65.0, 100.0)]
        chapters = build_chapters(qsos, windows)
        assert chapters.count("QSO") == 1  # second is only 5s after the first

    def test_build_srt_matches_chapter_label_and_caps_duration(self):
        # The cue shows exactly the chapter label -- call + band/mode + dup
        # tag -- and nothing else (no locator/distance/serials/reports).
        qsos = [
            Qso(
                datetime(2026, 7, 4, 11, 0, 0),
                "HG7F",
                "599",
                "001",
                "599",
                "010",
                "JN97KR",
                26,
                True,
                band="2M",
                mode="CW",
            )
        ]
        windows = [(10.0, 70.0)]  # far longer than CAPTION_DUR_S
        srt = build_srt(qsos, windows)
        assert f"00:00:10,000 --> 00:00:{10 + int(CAPTION_DUR_S):02d},000" in srt
        cue = srt.strip().splitlines()[-1]
        assert cue == "QSO 001 HG7F  2M CW (dup)"
        # the dropped extras must not appear
        assert "JN97KR" not in srt and "km" not in srt and "RX" not in srt

    def test_build_srt_cue_equals_chapter_body(self):
        # Guards the shared _qso_label: an SRT cue is byte-identical to the
        # chapter line's text (everything after the timestamp).
        qsos = [
            Qso(
                datetime(2026, 7, 4, 11, 0, 0),
                "HG7F",
                "599",
                "001",
                "599",
                "010",
                "JN97KR",
                26,
                False,
                band="70CM",
                mode="FM",
            )
        ]
        windows = [(60.0, 120.0)]
        chapter_body = (
            build_chapters(qsos, windows).strip().splitlines()[-1].split(" ", 1)[1]
        )
        srt_cue = build_srt(qsos, windows).strip().splitlines()[-1]
        assert srt_cue == chapter_body == "QSO 001 HG7F  70CM FM"


class TestWavMetadata:
    def test_parse_ssb(self):
        title = (
            "IC-9700 Voice Recorder Data   144.299.84 USB    "
            "----.---.-- ------ -- TX 2026-07-06 16:00:37"
        )
        assert parse_wav_title(title) == (144299840, "SSB", True)

    def test_parse_cw(self):
        title = (
            "IC-9700 Voice Recorder Data   144.080.00 CW     "
            "----.---.-- ------ -- TX 2026-07-06 16:03:24"
        )
        assert parse_wav_title(title) == (144080000, "CW", True)

    def test_parse_fm_rx(self):
        title = (
            "IC-9700 Voice Recorder Data   145.350.00 FM     "
            "----.---.-- ------ -- RX 2026-07-06 16:49:24"
        )
        assert parse_wav_title(title) == (145350000, "FM", False)

    def test_parse_lsb_normalizes_to_ssb(self):
        title = (
            "IC-9700 Voice Recorder Data   432.109.75 LSB    "
            "----.---.-- ------ -- RX 2026-07-06 16:37:24"
        )
        freq_hz, mode, ptt = parse_wav_title(title)
        assert mode == "SSB"

    def test_parse_returns_none_for_unrecognized_format(self):
        assert parse_wav_title("not an IC-9700 title at all") is None
        assert parse_wav_title("") is None

    def test_read_wav_metadata_populates_segment(self, tmp_path):
        path = tmp_path / "seg.wav"
        _write_wav_with_title(
            path,
            "IC-9700 Voice Recorder Data   144.080.00 CW     "
            "----.---.-- ------ -- TX 2026-07-06 16:03:24",
        )
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz == 144080000
        assert segs[0].mode == "CW"
        assert segs[0].ptt is True

    def test_read_wav_metadata_leaves_none_without_a_tag(self, tmp_path):
        path = tmp_path / "plain.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 100)
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz is None
        assert segs[0].mode is None
        assert segs[0].ptt is None


class TestTelemetryAlignment:
    def test_load_telemetry_parses_lines_and_skips_bad_ones(self, tmp_path):
        f = tmp_path / "telem.jsonl"
        f.write_text(
            '{"t": "2026-07-04T11:00:02Z", "freq_hz": 144174000, '
            '"mode": "CW", "az": 135.0}\n'
            "not json\n"
            '{"t": "2026-07-04T11:00:05Z", "freq_hz": null}\n'
        )
        samples = load_telemetry(str(f))
        assert len(samples) == 2
        assert samples[0] == TelemetrySample(
            datetime(2026, 7, 4, 11, 0, 2), 144174000, "CW", 135.0
        )
        assert samples[1].freq_hz is None

    def test_load_telemetry_accepts_microsecond_timestamps(self, tmp_path):
        # The logger's telemetry is written from the icom_net push callback
        # and the rotator poller as they happen, with the same microsecond
        # stamps as the input log -- not the whole seconds a 1 Hz sampler
        # produced. Older recordings still carry whole-second stamps, so
        # both have to parse; a strict whole-second format silently drops
        # every line of a new recording via the ValueError branch above.
        f = tmp_path / "telem.jsonl"
        f.write_text(
            '{"t": "2026-07-04T11:00:02.123456Z", "freq_hz": 144174000, '
            '"mode": "CW"}\n'
            '{"t": "2026-07-04T11:00:05Z", "az": 135.0}\n'
        )
        samples = load_telemetry(str(f))
        assert len(samples) == 2
        assert samples[0].t == datetime(2026, 7, 4, 11, 0, 2, 123456)
        assert samples[0].freq_hz == 144174000
        assert samples[0].az is None  # a rig event carries no az at all
        assert samples[1].t == datetime(2026, 7, 4, 11, 0, 5)
        assert samples[1].az == 135.0

    def _wav_seg(self, wall, dur, audio_t, freq_hz, mode, ptt):
        s = Segment("a", wall, dur, audio_t)
        s.freq_hz, s.mode, s.ptt = freq_hz, mode, ptt
        return s

    def test_ptt_comes_from_wav_metadata_regardless_of_telemetry(self):
        # ptt never needs telemetry any more -- it's ground truth straight
        # from the WAV file itself (see build_state_events' docstring for
        # why: unlike freq/mode, ptt cannot legitimately change mid-segment,
        # so the WAV metadata alone is always sufficient and telemetry's own
        # up-to-1-second polling lag is no longer a concern at all).
        segs = [
            self._wav_seg(
                datetime(2026, 7, 6, 16, 0, 37), 2.214, 142.533, 144299840, "SSB", True
            )
        ]
        [(start, end, st)] = build_state_events(segs, [], offset_h=0)
        assert start == 142.533  # exactly the WAV segment boundary
        assert end == 142.533 + 2.214
        assert st.ptt is True
        assert st.freq_hz == 144299840
        assert st.mode == "SSB"

    def test_wav_value_used_for_whole_segment_without_telemetry_change(self):
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0, 144174000, "CW", True
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 2), 144174000, "CW", 135.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 5), 144174000, "CW", 136.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 8), 144174000, "CW", 137.0),
        ]
        [(start, end, st)] = build_state_events(segs, telemetry, offset_h=2)
        assert (start, end) == (0.0, 10.0)
        assert st.freq_hz == 144174000
        assert st.mode == "CW"
        assert st.az == 136.0  # median of 135/136/137

    def test_az_carries_forward_into_a_run_with_no_az_sample(self):
        # Telemetry is change-only now: the rotator poller writes a line
        # when the azimuth actually moves, not once a second regardless.
        # A rotator parked on one bearing for a whole QSO therefore leaves
        # zero az samples inside that segment's span -- taking the median
        # of nothing yields None, which renders as "ROT ---" even though
        # the rotator was online and pointing somewhere known the whole
        # time. az is a step function: it holds until the next event.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0, 144174000, "CW", True
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 10, 55, 0), None, None, 135.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 0), 144174000, "CW", None),
        ]
        [(_, _, st)] = build_state_events(segs, telemetry, offset_h=2)
        assert st.az == 135.0

    def test_az_carried_forward_per_run_not_just_per_segment(self):
        # The carry-forward is evaluated at each run's own start, against
        # every az event so far -- not once per segment. A QSY mid-segment
        # splits it into two runs; the second run inherits the azimuth set
        # during the *first* run, not the one from before the segment began.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0, 144300000, "SSB", False
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 10, 59, 0), None, None, 90.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 2), None, None, 200.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 6), 432200000, "CW", None),
        ]
        events = build_state_events(segs, telemetry, offset_h=2)
        assert [e[2].az for e in events] == [200.0, 200.0]

    def test_explicit_az_null_ends_the_carry_forward(self):
        # The rotator going offline is itself an event: the logger writes one
        # explicit {"az": null} line at the transition and then stays quiet.
        # That null has to *terminate* the carry-forward -- treating it as
        # "this record just doesn't mention az" would sail straight past the
        # rotator dying and keep showing its last bearing for the rest of the
        # video, which is exactly the stale reading the badge must not show.
        # ROT --- is the honest answer, matching the logger's own toolbar.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0, 144300000, "SSB", False
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 10, 59, 0), None, None, 135.0),
            TelemetrySample(
                datetime(2026, 7, 4, 10, 59, 30), None, None, None, az_offline=True
            ),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 5), 432200000, "CW", None),
        ]
        assert [e[2].az for e in build_state_events(segs, telemetry, offset_h=2)] == [
            None,
            None,
        ]

    def test_a_rig_event_does_not_count_as_an_az_reading(self):
        # The mirror image: a rig event carries no "az" key at all, which is
        # silence about the rotator, not a report that it went offline. It
        # must not terminate the carry-forward the way an explicit null does.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0, 144300000, "SSB", False
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 10, 59, 0), None, None, 135.0),
            TelemetrySample(datetime(2026, 7, 4, 10, 59, 30), 144300000, "SSB", None),
        ]
        [(_, _, st)] = build_state_events(segs, telemetry, offset_h=2)
        assert st.az == 135.0

    def test_load_telemetry_distinguishes_absent_az_from_null_az(self, tmp_path):
        # Both land as az=None, but they mean opposite things: an absent key
        # is silence, an explicit null is "the rotator went offline".
        f = tmp_path / "telem.jsonl"
        f.write_text(
            '{"t": "2026-07-04T11:00:00.000000Z", "freq_hz": 144174000, "mode": "CW"}\n'
            '{"t": "2026-07-04T11:00:01.000000Z", "az": null}\n'
        )
        silent, offline = load_telemetry(str(f))
        assert (silent.az, silent.az_offline) == (None, False)
        assert (offline.az, offline.az_offline) == (None, True)

    def test_small_wav_telemetry_disagreement_does_not_split(self):
        # Regression test for a real bug found right after switching to WAV
        # metadata as the seed: the WAV's own frequency and rigctld's (via
        # telemetry) don't agree to the exact Hz even when nothing changed
        # -- checked against the real July round's data, a systematic
        # disagreement of 160/250/300/310 Hz (depending on band) shows up
        # on nearly every segment's very first telemetry sample. Comparing
        # them exactly turned that into a spurious extra run at the start
        # of almost every segment. Real genuine retunes in the same data
        # are >=1000 Hz (mostly round kHz steps) -- a clean gap, zero
        # occurrences between 310 Hz and 1000 Hz.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 6, 16, 0, 37), 2.214, 142.533, 144299840, "SSB", True
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 6, 16, 0, 37), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 16, 0, 38), 144300000, "SSB", None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        assert len(events) == 1
        assert events[0][2].freq_hz == 144299840  # stayed on the WAV's own value

    def test_long_segment_splits_on_a_real_frequency_change(self):
        # Regression test for the original reported bug: a long idle/
        # listening segment (no PTT to split the WAV on) where the operator
        # QSY'd partway through used to get ONE majority-voted state for
        # its entire span. Real values from the July round: SSB 144.300 MHz
        # held 16:05:25-16:05:28, then a CW QSY through
        # 432.080/.088/.179/.199/.200 MHz -- each step far larger than the
        # WAV/telemetry disagreement tolerance, so still correctly detected.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 6, 13, 0, 0), 11.0, 0.0, 144300000, "SSB", False
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 0), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 1), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 2), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 3), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 4), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 5), 432080000, "CW", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 6), 432088000, "CW", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 7), 432179000, "CW", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 8), 432199000, "CW", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 9), 432199000, "CW", None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 10), 432200000, "CW", None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        [ev] = [e for e in events if e[0] <= 6.0 < e[1]]
        assert ev[2].freq_hz == 432088000
        assert ev[2].mode == "CW"
        assert not any(e[2].freq_hz == 144300000 and e[0] <= 6.0 < e[1] for e in events)

    def test_segment_without_wav_metadata_produces_no_event(self):
        # No WAV tag at all (freq_hz/mode/ptt all None) -- skipped rather
        # than guessed at from telemetry alone.
        segs = [Segment("a", datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 2), 144174000, "CW", 135.0)
        ]
        assert build_state_events(segs, telemetry, offset_h=0) == []

    def test_a_momentary_none_reading_does_not_split_a_run(self):
        # A single dropped rigctld poll shouldn't fragment an otherwise
        # stable state into spurious extra badge events.
        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0, 144174000, "CW", True
            )
        ]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 0), 144174000, "CW", None),
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 1), None, None, None),
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 2), 144174000, "CW", None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        assert len(events) == 1
        assert events[0][2].freq_hz == 144174000


def _text(t, text):
    return InputLogEvent(t, "text", text=text)


def _qso_ev(t, call, dup=False):
    return InputLogEvent(t, "qso", call=call, dup=dup)


class TestInputLog:
    def test_load_input_log_parses_both_event_kinds(self, tmp_path):
        f = tmp_path / "input.jsonl"
        f.write_text(
            '{"t": "2026-07-04T11:00:02.123456Z", "event": "text", "text": "H"}\n'
            "not json\n"
            '{"t": "2026-07-04T11:00:05.000000Z", "event": "qso", "call": "HA7NS", "dup": false}\n'
        )
        log = load_input_log(str(f))
        assert log == [
            InputLogEvent(datetime(2026, 7, 4, 11, 0, 2, 123456), "text", text="H"),
            InputLogEvent(
                datetime(2026, 7, 4, 11, 0, 5), "qso", call="HA7NS", dup=False
            ),
        ]

    def test_load_input_log_defaults_missing_event_field_to_text(self, tmp_path):
        # Written before the "event" field existed, or hand-crafted -- treat
        # as a keystroke rather than dropping it.
        f = tmp_path / "input.jsonl"
        f.write_text('{"t": "2026-07-04T11:00:02.000000Z", "text": "H"}\n')
        log = load_input_log(str(f))
        assert log == [InputLogEvent(datetime(2026, 7, 4, 11, 0, 2), "text", text="H")]


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


class TestMatchQsoTimes:
    def _qso(self, dt, call):
        return Qso(dt, call, "59", "1", "59", "2", "JN97MM", 10, False)

    def test_matches_by_call_for_a_single_occurrence(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS")]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 42, 123456), "HA7NS")]
        [t] = match_qso_times(qsos, log)
        assert t == datetime(2026, 7, 6, 16, 1, 42, 123456)

    def test_matches_across_a_hand_edited_minute_boundary(self):
        # A seeded skeleton (--seed-input-log) starts with the EDI's own
        # minute, but the whole point is the operator then edits 't' to the
        # real time from the audio -- which can easily land in a different
        # minute than the EDI recorded (e.g. the over started well before
        # Enter was pressed). Matching must not depend on the two agreeing.
        qsos = [self._qso(datetime(2026, 7, 6, 16, 5), "HA3KHB")]
        log = [
            _qso_ev(datetime(2026, 7, 6, 16, 1, 42), "HA3KHB")
        ]  # edited 4 minutes earlier
        [t] = match_qso_times(qsos, log)
        assert t == datetime(2026, 7, 6, 16, 1, 42)

    def test_none_when_no_input_log(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS")]
        assert match_qso_times(qsos, []) == [None]

    def test_none_for_unmatched_call(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS")]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 10), "HA3KHB")]
        assert match_qso_times(qsos, log) == [None]

    def test_text_events_are_not_candidates(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS")]
        log = [_text(datetime(2026, 7, 6, 16, 1, 10), "HA7NS 59 001")]
        assert match_qso_times(qsos, log) == [None]

    def test_repeated_call_resolved_in_encounter_order(self):
        # Same call worked twice (e.g. two different bands) -- the two
        # 'qso' events must not both map to the first QSO.
        qsos = [
            self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS"),
            self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS"),
        ]
        log = [
            _qso_ev(datetime(2026, 7, 6, 16, 1, 10), "HA7NS"),
            _qso_ev(datetime(2026, 7, 6, 16, 1, 50), "HA7NS"),
        ]
        times = match_qso_times(qsos, log)
        assert times == [
            datetime(2026, 7, 6, 16, 1, 10),
            datetime(2026, 7, 6, 16, 1, 50),
        ]


class TestQsoWindowsPreciseAnchor:
    def test_precise_time_used_as_snap_anchor_instead_of_edi_minute(self):
        # Burst starts at 26.0s; the EDI-minute-derived approx time would
        # map to audio_t=0 (wall-clock rounds down to the segment start),
        # landing _snap_to_cluster on the wrong (or no) earlier cluster. An
        # exact submit time mapping into the real burst fixes the anchor.
        segs = [
            Segment("a", datetime(2026, 7, 6, 16, 1, 0), 26.0, 0.0),  # gap
            Segment(
                "b",
                datetime(2026, 7, 6, 16, 1, 26),
                5.0,
                26.0,
                events=[CharEvent(0.5, "H")],
            ),  # the real over
        ]
        q = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA7NS",
            "59",
            "1",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        precise = datetime(2026, 7, 6, 16, 1, 28)  # submitted 2s into the over
        [(start, _end)] = qso_windows(
            [q], segs, offset_h=0, total=31.0, qso_times=[precise]
        )
        assert start == 26.0

    def test_falls_back_to_edi_time_when_unmatched(self):
        segs = [
            Segment(
                "a",
                datetime(2026, 7, 6, 16, 1, 0),
                10.0,
                0.0,
                events=[CharEvent(0.5, "H")],
            )
        ]
        q = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA7NS",
            "59",
            "1",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        without = qso_windows([q], segs, offset_h=0, total=10.0)
        with_none = qso_windows([q], segs, offset_h=0, total=10.0, qso_times=[None])
        assert without == with_none

    def test_panel_clears_at_its_own_finish_not_the_next_qsos_start(self):
        # Regression test for a real reported bug: a QSO's panel used to
        # stay up until the *next* QSO's panel appeared (or the clip ended,
        # for the last QSO) -- but the input log's 'qso' events tell us
        # exactly when a QSO finished, so there's no need to guess that
        # part at all, only the start. Two QSOs in genuinely separate
        # bursts (a real ~50s gap between them, unlike the shared-burst
        # case) must each clear at their own finish, leaving a real gap
        # with nothing shown in between, and the last one must clear well
        # before the clip's end rather than lingering to `total`.
        segs = [
            Segment(
                "a",
                datetime(2026, 7, 6, 16, 1, 0),
                5.0,
                0.0,
                events=[CharEvent(0.5, "H")],
            ),
            Segment("b", datetime(2026, 7, 6, 16, 1, 5), 50.0, 5.0),  # real gap
            Segment(
                "c",
                datetime(2026, 7, 6, 16, 1, 55),
                5.0,
                55.0,
                events=[CharEvent(0.5, "H")],
            ),
        ]
        q1 = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA7NS",
            "59",
            "1",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        q2 = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA3KHB",
            "59",
            "2",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        times = [datetime(2026, 7, 6, 16, 1, 3), datetime(2026, 7, 6, 16, 1, 58)]
        windows = qso_windows([q1, q2], segs, offset_h=0, total=70.0, qso_times=times)
        assert windows == [(0.0, 3.0), (55.0, 58.0)]

    def test_qsos_sharing_one_burst_get_distinct_non_overlapping_windows(self):
        # Regression test for a real reported bug: the same station worked
        # on multiple modes back-to-back (e.g. SSB then FM then CW) with no
        # real listening gap between them is *one* burst as far as
        # cluster_starts is concerned -- there's no audio structure to tell
        # the individual overs apart. Snapping every one of those QSOs onto
        # that single shared cluster start collapsed their panels onto the
        # same instant; the old minimum-1-second window then showed two
        # panels on screen simultaneously for that one second, and the
        # first one vanished before its own real submit time.
        #
        # QSO 1's window now ends exactly at its own real finish (28.0, its
        # qso_times entry) rather than lingering until QSO 2's finish -- a
        # second real bug found later: a QSO's panel should clear once it's
        # actually done, known exactly from the input log, not stay up
        # until the next QSO's panel appears. QSO 2 then starts exactly
        # where QSO 1 left off (chained, since there's no audio boundary
        # between them) and itself ends at its own real finish (29.0).
        segs = [
            Segment("a", datetime(2026, 7, 6, 16, 1, 0), 26.0, 0.0),  # gap
            Segment(
                "b",
                datetime(2026, 7, 6, 16, 1, 26),
                5.0,
                26.0,
                events=[CharEvent(0.5, "H")],
            ),  # the whole shared burst
        ]
        q1 = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA3KHB",
            "59",
            "1",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        q2 = Qso(
            datetime(2026, 7, 6, 16, 1),
            "HA3KHB",
            "59",
            "2",
            "59",
            "2",
            "JN97MM",
            10,
            False,
        )
        times = [datetime(2026, 7, 6, 16, 1, 28), datetime(2026, 7, 6, 16, 1, 29)]
        windows = qso_windows([q1, q2], segs, offset_h=0, total=31.0, qso_times=times)
        assert windows == [(26.0, 28.0), (28.0, 29.0)]
        # explicitly: no overlap, no gap, and QSO 2 clears well before `total`
        (s1, e1), (s2, e2) = windows
        assert e1 == s2
        assert e2 < 31.0


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
            webcam=str(tmp_path / "cam.mp4"),
            webcam_start=2.0,
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


class TestStreamPrecedesAudio:
    """A cast/scope stream that began *before* the first WAV segment must be
    entered partway in, not clamped to video t=0 -- the clamp showed up as
    the cast PiP's clock lagging the session by exactly cast-to-WAV gap
    (25 s in the dry-run that caught it). run-recorded-contest-session.sh
    guarantees this ordering: asciinema starts before the radio recorder."""

    def _segs(self):
        return [cv.Segment("a.wav", datetime(2026, 8, 6, 19, 16, 0), 330.0, 0.0)]

    def test_stream_start_is_negative_before_first_segment(self):
        assert cv.stream_start(datetime(2026, 8, 6, 19, 15, 35), self._segs()) == -25.0

    def test_stream_start_matches_audio_time_for_inside_the_recording(self):
        wall = datetime(2026, 8, 6, 19, 17, 0)
        segs = self._segs()
        assert cv.stream_start(wall, segs) == cv.audio_time_for(wall, segs)

    def test_render_enters_cast_partway_on_negative_start(self, monkeypatch, tmp_path):
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
            cast_start=-25.0,
        )
        cmd = captured["cmd"]
        i = cmd.index("-ss")
        assert cmd[i + 1] == "25.000"
        assert cmd[i + 2] == "-i"  # the seek applies to the cast input
        assert "-25.000" not in cmd  # never a negative itsoffset

    def test_render_enters_scope_partway_on_negative_start(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setattr(
            cv.subprocess, "run", lambda cmd, check=True: captured.update(cmd=cmd)
        )
        cv.render(
            str(tmp_path / "a.wav"),
            str(tmp_path / "out.mp4"),
            1280,
            720,
            scope=str(tmp_path / "scope.mp4"),
            scope_start=-19.0,
            scope_end=300.0,
        )
        cmd = captured["cmd"]
        i = cmd.index("-ss")
        assert cmd[i + 1] == "19.000"
        assert cmd[i + 2] == "-i"
        assert "-19.000" not in cmd


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------


def _hud_seg(dur=600.0, audio_t=0.0, wall=None):
    return Segment("a", wall or datetime(2026, 8, 3, 20, 0, 0), dur, audio_t)


def _hud_qso(call, pts, loc="JN97TF", dup=False):
    return Qso(
        datetime(2026, 8, 3, 18, 0), call, "599", "001", "599", "001", loc, pts, dup
    )


class TestHudGeo:
    def test_rejects_anything_that_is_not_a_locator(self):
        assert cv.maidenhead_to_latlon("") is None
        assert cv.maidenhead_to_latlon("ZZ99") is None  # field letters stop at R
        assert cv.maidenhead_to_latlon("JN9") is None
        assert cv.maidenhead_to_latlon("JN97TF") is not None

    def test_six_character_locator_sits_inside_its_own_four_character_square(self):
        lat4, lon4 = cv.maidenhead_to_latlon("JN97")
        lat6, lon6 = cv.maidenhead_to_latlon("JN97TF")
        assert abs(lat6 - lat4) < 0.5
        assert abs(lon6 - lon4) < 1.0

    def test_initial_bearing_matches_the_cardinal_directions(self):
        assert abs(cv.initial_bearing(0, 0, 10, 0) - 0) < 0.1  # due north
        assert abs(cv.initial_bearing(0, 0, 0, 10) - 90) < 0.1  # due east
        assert abs(cv.initial_bearing(0, 0, -10, 0) - 180) < 0.1  # due south


class TestHudQsoMarks:
    def test_accumulates_score_count_and_best_dx_at_each_qso_end(self):
        qsos = [
            _hud_qso("HA1A", 100),
            _hud_qso("HA2B", 300),
            _hud_qso("HA3C", 0, dup=True),
        ]
        windows = [(0.0, 10.0), (20.0, 30.0), (40.0, 50.0)]
        assert cv.hud_qso_marks(qsos, windows) == [
            (10.0, 100, 1, 100),
            (30.0, 400, 2, 300),
            (50.0, 400, 3, 300),  # a dup adds a QSO but no score and no best DX
        ]

    def test_marks_are_ordered_by_window_end_not_by_edi_order(self):
        # qso_windows can hand back a QSO whose exact submit time reorders it
        # relative to the EDI's minute-precision sort.
        qsos = [_hud_qso("HA1A", 100), _hud_qso("HA2B", 300)]
        windows = [(30.0, 40.0), (0.0, 10.0)]
        assert [m[0] for m in cv.hud_qso_marks(qsos, windows)] == [10.0, 40.0]


class TestHudTimeline:
    def test_score_counts_up_over_the_animation_window_then_holds(self):
        tl = cv.HudTimeline(
            segs=[_hud_seg()], qso_marks=[(10.0, 100, 1, 100), (20.0, 400, 2, 300)]
        )
        assert tl.at(9.9).score == 0
        assert tl.at(10.0).score == 0  # the count-up starts from the old total
        midway = tl.at(10.0 + cv.HUD_SCORE_ANIM_S / 2).score
        assert 0 < midway < 100
        assert tl.at(10.0 + cv.HUD_SCORE_ANIM_S).score == 100
        assert tl.at(19.0).score == 100  # holds until the next QSO
        assert tl.at(20.0 + cv.HUD_SCORE_ANIM_S).score == 400

    def test_score_flash_decays_to_zero_over_the_same_window(self):
        tl = cv.HudTimeline(segs=[_hud_seg()], qso_marks=[(10.0, 100, 1, 100)])
        assert tl.at(10.0).score_flash == 1.0
        assert tl.at(10.0 + cv.HUD_SCORE_ANIM_S).score_flash < 1e-6
        assert tl.at(30.0).score_flash == 0.0

    def test_rate_counts_only_qsos_inside_the_trailing_window(self):
        window = cv.HUD_RATE_WINDOW_S
        tl = cv.HudTimeline(
            segs=[_hud_seg(dur=window * 2)],
            qso_marks=[(0.0, 1, 1, 1), (100.0, 2, 2, 1), (window + 50.0, 3, 3, 1)],
        )
        # At window+60 the first QSO has aged out; two remain inside.
        assert tl.at(window + 60.0).rate_per_h == 2 * 3600.0 / window

    def test_target_bearing_only_shows_inside_its_own_qso_window(self):
        tl = cv.HudTimeline(segs=[_hud_seg()], target_spans=[(10.0, 20.0, 271.0)])
        assert tl.at(9.0).target_az is None
        assert tl.at(15.0).target_az == 271.0
        assert tl.at(20.0).target_az is None

    def test_rig_state_supplies_band_from_the_frequency(self):
        events = [(0.0, 10.0, SegState(ptt=True, freq_hz=432_200_000, mode="CW"))]
        tl = cv.HudTimeline(segs=[_hud_seg()], state_events=events)
        state = tl.at(5.0)
        assert (state.ptt, state.mode, state.band) == (True, "CW", "70CM")
        assert tl.at(15.0).band is None  # past the run, nothing carries over

    def test_signal_level_clears_when_the_scope_recording_stops(self):
        tl = cv.HudTimeline(segs=[_hud_seg()], s_marks=[(5.0, 0.5)])
        assert tl.at(5.0).s_level == 0.5
        assert tl.at(5.0 + cv.HUD_S_HOLD_S).s_level == 0.5
        assert tl.at(5.0 + cv.HUD_S_HOLD_S + 0.1).s_level is None

    def test_utc_is_the_local_wall_clock_less_the_derived_offset(self):
        tl = cv.HudTimeline(segs=[_hud_seg()], offset_h=2)
        assert tl.at(30.0).utc == datetime(2026, 8, 3, 18, 0, 30)


class TestHudSources:
    def test_s_marks_read_the_scope_sweeps_own_centre_bins(self):
        segs = [_hud_seg()]  # 20:00 local == 18:00 UTC at offset 2
        ts = datetime(2026, 8, 3, 18, 0, 30, tzinfo=timezone.utc).timestamp()
        quiet = bytes([10] * 475)
        loud = bytearray([10] * 475)
        loud[475 // 2] = SCOPE_AMP_MAX
        marks = cv.hud_s_marks(
            [(ts, 0, 0, quiet), (ts + 1, 0, 0, bytes(loud))], segs, offset_h=2
        )
        assert marks[0] == (30.0, 10 / SCOPE_AMP_MAX)
        assert marks[1] == (31.0, 1.0)

    def test_s_marks_take_the_loudest_centre_bin_not_the_average(self):
        # A signal sitting in one bin must not be diluted by the quiet bins
        # either side of it.
        segs = [_hud_seg()]
        ts = datetime(2026, 8, 3, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        pixels = bytearray([0] * 475)
        pixels[475 // 2] = 80
        marks = cv.hud_s_marks([(ts, 0, 0, bytes(pixels))], segs, offset_h=2)
        assert marks[0][1] == 80 / SCOPE_AMP_MAX

    def test_target_spans_give_the_bearing_to_each_worked_station(self):
        # JN87 sits west-north-west of JN97TF (2.6 degrees of longitude
        # west, a quarter degree north), i.e. a bearing just short of 280.
        spans = cv.hud_target_spans(
            [_hud_qso("HA1A", 100, loc="JN87")], [(0.0, 10.0)], "JN97TF"
        )
        assert len(spans) == 1
        start, end, az = spans[0]
        assert (start, end) == (0.0, 10.0)
        assert 275 < az < 285

    def test_target_spans_skip_a_qso_whose_locator_will_not_parse(self):
        spans = cv.hud_target_spans(
            [_hud_qso("HA1A", 100, loc="?????")], [(0.0, 10.0)], "JN97TF"
        )
        assert spans == []

    def test_wall_time_at_inverts_audio_time_for(self):
        segs = [
            Segment("a", datetime(2026, 8, 3, 20, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 8, 3, 20, 1, 0), 60.0, 60.0),
        ]
        for t in (0.0, 30.0, 59.0, 60.0, 90.0):
            assert cv.audio_time_for(cv.wall_time_at(t, segs), segs) == t


class TestHudLayout:
    def test_slots_stay_inside_the_bar_and_never_overlap(self):
        # A plain left-to-right check is not enough any more: PWR/STATS sit
        # above the CW ticker and so share their x range with it.
        rects = list(cv.HUD_SLOTS.values())
        for x, y, w, h in rects:
            assert x >= 0 and y >= 0
            assert x + w <= cv.HUD_W
            assert y + h <= cv.HUD_H
        for i, (ax, ay, aw, ah) in enumerate(rects):
            for bx, by, bw, bh in rects[i + 1 :]:
                apart = ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay
                assert apart, f"{(ax, ay, aw, ah)} overlaps {(bx, by, bw, bh)}"

    def test_layout_scales_to_another_bar_size(self):
        scaled = cv.hud_layout(cv.HUD_W // 2, cv.HUD_H // 2)
        x, y, w, h = scaled["score"]
        assert (x, y, w, h) == tuple(v // 2 for v in cv.HUD_SLOTS["score"])


class TestHudDrawing:
    def test_frame_has_the_requested_size(self):
        assert cv.draw_hud_frame(cv.hud_demo_state(), 1280, 174).size == (1280, 174)

    def test_an_empty_state_renders_placeholders_rather_than_crashing(self):
        # Every recording made before the meter recorder existed looks like
        # this for the PWR panel, and a --duration cut before the first QSO
        # looks like it for the rest.
        assert cv.draw_hud_frame(cv.HudState()).size == (cv.HUD_W, cv.HUD_H)

    def test_face_slot_is_left_empty_for_the_webcam_composite(self):
        img = cv.draw_hud_frame(cv.hud_demo_state())
        x, y, w, h = cv.HUD_SLOTS["face"]
        inner = img.crop((x + 16, y + 16, x + w - 16, y + h - 16))
        assert len(inner.getcolors(maxcolors=4)) == 1  # one flat colour, nothing drawn

    def test_score_is_fitted_to_its_panel_instead_of_overflowing(self):
        # Regression: a fixed point size overflowed the SCORE panel the moment
        # the score reached five digits, spilling red digits across the gutter
        # into the QSOS panel. Confirmed red by monkeypatching _fit_font back
        # to a plain fixed-size lookup, which fails this assertion.
        state = cv.hud_demo_state()
        state.score = 123456
        img = cv.draw_hud_frame(state)
        sx, _, sw, _ = cv.HUD_SLOTS["score"]
        gutter = img.crop((sx + sw, 0, cv.HUD_SLOTS["qsos"][0], cv.HUD_H))
        assert np.asarray(gutter)[:, :, 0].max() < 60  # no lit digits here


class TestHudMatrixFont:
    def test_every_character_the_decoder_can_emit_has_a_glyph(self):
        # MORSE is the complete set the CW decoder can ever produce, so the
        # font is finite and fully determined -- nothing can arrive that the
        # ticker has no glyph for.
        assert set(cv.MORSE.values()) <= set(cv._FONT_5X7)

    def test_every_glyph_is_exactly_five_by_seven_bits(self):
        # A mistyped row is a plausible-looking glyph rather than an error, so
        # the shape of the table is checked here and the glyphs themselves were
        # verified by rendering the whole set as a sheet and reading it.
        for ch, bits in cv._FONT_5X7.items():
            rows = bits.split()
            assert len(rows) == cv.HUD_MATRIX_ROWS, ch
            assert all(len(r) == cv.HUD_MATRIX_COLS for r in rows), ch
            assert set("".join(rows)) <= {"0", "1"}, ch

    def test_an_unknown_character_falls_back_to_a_question_mark(self):
        assert cv._matrix_rows("\u00e9") == cv._matrix_rows("?")


class TestHudChromeSplit:
    def test_static_labels_are_not_drawn_over_supplied_artwork(self):
        # The artwork bakes every static label, so drawing them again would
        # print each one twice. Regression: the stats captions (UTC / RATE /H
        # / ODX KM) used to be drawn in the value path and would have doubled.
        art = Image.new("RGB", (cv.HUD_W, cv.HUD_H), (0, 0, 0))
        img = cv.draw_hud_frame(cv.hud_demo_state(), background=art)
        x, y, w, h = cv.HUD_SLOTS["score"]
        label_strip = img.crop((x, y + h - 60, x + w, y + h))
        assert np.asarray(label_strip).max() < 40

    def test_the_placeholder_does_draw_them(self):
        # ... and without artwork the placeholder has to stand in for it,
        # otherwise the preview would show unlabelled numbers.
        img = cv.draw_hud_frame(cv.hud_demo_state())
        x, y, w, h = cv.HUD_SLOTS["score"]
        label_strip = img.crop((x, y + h - 60, x + w, y + h))
        assert np.asarray(label_strip).max() > 100

    def test_values_are_drawn_either_way(self):
        art = Image.new("RGB", (cv.HUD_W, cv.HUD_H), (0, 0, 0))
        img = cv.draw_hud_frame(cv.hud_demo_state(), background=art)
        x, y, w, h = cv.HUD_SLOTS["score"]
        assert np.asarray(img.crop((x, y, x + w, y + h - 60)))[:, :, 0].max() > 100


class TestMeterCalibration:
    def test_vd_matches_the_multimeter_reading_it_was_checked_against(self):
        # Raw 152 was measured on the real radio while a multimeter read
        # 13.78 V -- Icom's own Vd curve lands within 1%.
        assert abs(cv.vd_volts(152) - 13.78) < 0.15

    def test_po_and_swr_hit_their_published_calibration_points(self):
        assert cv.po_percent(213) == 100.0
        assert cv.po_percent(143) == 50.0
        assert cv.swr_ratio(0) == 1.0
        assert cv.swr_ratio(48) == 1.5
        assert cv.swr_ratio(120) == 3.0

    def test_id_uses_the_measured_line_not_icoms_curve(self):
        # Icom's IC-7300 curve gives 17.6 A for raw 171. Measured against a
        # multimeter in series, PA drain fits a line through the origin at
        # 0.0741 A/raw -- ~12.7 A there, and ~17.9 A full scale, not 25 A.
        assert cv.id_amps(0) == 0.0
        assert abs(cv.id_amps(171) - 12.67) < 0.1
        # The low-current cluster the line was fitted through. The bound is
        # 6% because the lowest point sits 5.3% off: a 20 A meter range
        # resolves ~5 A poorly, and the constant-receive-baseline assumption
        # is least safe there.
        for raw, amps in ((55, 3.87), (61, 4.48), (64, 4.71)):
            assert abs(cv.id_amps(raw) - amps) / amps < 0.06

    def test_id_stays_linear_through_zero(self):
        # Two points a factor of three apart in current agreed to 1% on the
        # same through-origin slope, so a curve that bends is a regression.
        assert abs(cv.id_amps(120) - 2 * cv.id_amps(60)) < 0.01

    def test_a_missing_reading_stays_missing_rather_than_becoming_zero(self):
        # An old recording has no meter data at all; the PWR panel must show
        # its placeholder rather than a confident 0.0 V.
        assert cv.vd_volts(None) is None
        assert cv.id_amps(None) is None

    def test_meters_reach_the_hud_state(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(
            '{"t": "2026-08-03T18:00:30.000000Z", "vd": 152, "id": 171,'
            ' "swr": 28, "po": 213}\n'
        )
        telemetry = cv.load_telemetry(str(f))
        assert (telemetry[0].vd, telemetry[0].id_raw) == (152, 171)
        tl = cv.HudTimeline(
            segs=[_hud_seg()],
            offset_h=2,
            meter_marks=cv.hud_meter_marks(telemetry, [_hud_seg()], 2),
        )
        assert tl.at(20.0).vd is None  # before the first reading
        assert abs(tl.at(60.0).vd - 13.78) < 0.15
        assert abs(tl.at(60.0).id_a - 12.67) < 0.1

    def test_a_radio_disconnect_clears_the_meters_rather_than_holding_them(
        self, tmp_path
    ):
        # A real session dropped three times in nine minutes. Meters are
        # change-only and a supply voltage has no reason to change, so without
        # an explicit null the pre-outage reading would be shown for the whole
        # outage.
        f = tmp_path / "t.jsonl"
        f.write_text(
            '{"t": "2026-08-03T18:00:30.000000Z", "vd": 152, "id": 171,'
            ' "swr": 28, "po": 213}\n'
            '{"t": "2026-08-03T18:01:00.000000Z", "vd": null, "id": null,'
            ' "swr": null, "po": null}\n'
        )
        telemetry = cv.load_telemetry(str(f))
        assert telemetry[1].meters_offline
        segs = [_hud_seg()]
        tl = cv.HudTimeline(
            segs=segs, offset_h=2, meter_marks=cv.hud_meter_marks(telemetry, segs, 2)
        )
        assert tl.at(45.0).vd is not None  # while the radio was there
        assert tl.at(120.0).vd is None  # and gone once it dropped
        assert tl.at(120.0).id_a is None


def _render_cmd(**kw):
    """render()'s ffmpeg command, without running it."""
    captured = {}

    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        return None

    real = cv.subprocess.run
    cv.subprocess.run = fake_run
    try:
        cv.render("a.wav", "o.mp4", 1920, 1080, **kw)
    finally:
        cv.subprocess.run = real
    return captured["cmd"]


class TestHudRender:
    def test_frame_key_ignores_time_but_tracks_everything_drawn(self):
        a = cv.hud_demo_state()
        b = cv.hud_demo_state()
        b.t = a.t + 5.0
        assert cv.hud_frame_key(a) == cv.hud_frame_key(b)  # t alone changes nothing
        b.score = a.score + 1
        assert cv.hud_frame_key(a) != cv.hud_frame_key(b)

    def test_frame_key_quantises_the_continuously_varying_values(self):
        # The meter is 18 discrete segments and a needle rounded to a degree
        # moves under a pixel; without this the scope-derived signal level
        # would force a fresh draw ~30 times a second for no visible gain.
        a = cv.hud_demo_state()
        b = cv.hud_demo_state()
        b.s_level = a.s_level + 0.001
        b.rot_az = a.rot_az + 0.2
        assert cv.hud_frame_key(a) == cv.hud_frame_key(b)
        b.s_level = a.s_level + 0.2
        assert cv.hud_frame_key(a) != cv.hud_frame_key(b)

    def test_bar_height_is_even_at_every_supported_resolution(self):
        # libx264 refuses an odd dimension and 720p rounds to 173. Found by
        # rendering a real 720p clip, not by any string-level assertion --
        # the 1080p reference height is already even.
        for _, H in cv.RESOLUTIONS.values():
            assert cv.hud_height(H) % 2 == 0

    def test_render_places_the_hud_bar_along_the_bottom(self):
        cmd = _render_cmd(hud="h.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "[hudbar]overlay=x=0:y=main_h-h" in graph

    def test_the_webcam_moves_into_the_face_recess_when_a_hud_is_present(self):
        # Bottom-right is under the bar now, so the corner PiP would be hidden.
        face = cv.hud_layout(1920, cv.hud_height(1080))["face"]
        graph = _render_cmd(hud="h.mp4", webcam="w.mp4")[
            _render_cmd(hud="h.mp4", webcam="w.mp4").index("-filter_complex") + 1
        ]
        assert f"scale={face[2]}:{face[3]}" in graph
        assert f"overlay=x={face[0]}:" in graph

    def test_without_a_hud_the_webcam_keeps_its_corner(self):
        graph = _render_cmd(webcam="w.mp4")[
            _render_cmd(webcam="w.mp4").index("-filter_complex") + 1
        ]
        assert "overlay=x=main_w-w-" in graph


def _cw_segs():
    return [
        Segment(
            "a", datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
            events=[CharEvent(0.5, "H"), CharEvent(0.6, "I")],
        )
    ]  # fmt: skip


def _ticker_at(t, segs, state_events=None, long_cw_spans=None):
    tl = cv.HudTimeline(
        segs=segs,
        stream=cv.ticker_stream(cv.ticker_chunks(segs, state_events, long_cw_spans)),
    )
    return "".join(ch for _, ch in tl.at(t).ticker)


class TestTickerScrolling:
    def test_characters_march_off_the_display_on_their_own(self):
        # The whole point of scrolling on a clock: no clearing rule, no flush,
        # no staleness horizon. A character keyed at t=0.5 has physically left
        # a HUD_TICKER_SPAN_S-wide display well before t=30, so the leak bugs
        # the old static transcript needed guarding against cannot occur.
        segs = _cw_segs()
        assert "H" in _ticker_at(1.0, segs)
        assert _ticker_at(cv.HUD_TICKER_SPAN_S + 2.0, segs) == ""

    def test_a_later_burst_never_shares_the_display_with_an_earlier_one(self):
        segs = [
            Segment(
                "a", datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0,
                events=[CharEvent(1.0, "A"), CharEvent(2.0, "B")],
            ),
            Segment("b", datetime(2026, 7, 4, 13, 0, 10), 474.0, 10.0),
            Segment(
                "c", datetime(2026, 7, 4, 13, 7, 4), 5.0, 484.0,
                events=[CharEvent(0.01, "X"), CharEvent(0.6, "Y")],
            ),
        ]  # fmt: skip
        shown = _ticker_at(484.5, segs)
        assert "X" in shown and "A" not in shown and "B" not in shown

    def test_fast_keying_queues_instead_of_overlapping(self):
        # Fed faster than it scrolls, a physical ticker queues characters one
        # cell apart rather than piling them on top of each other.
        seg = Segment(
            "a", datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
            events=[CharEvent(i * 0.01, c) for i, c in enumerate("ABCDE")],
        )  # fmt: skip
        tl = cv.HudTimeline(
            segs=[seg], stream=cv.ticker_stream(cv.ticker_chunks([seg], None, None))
        )
        offsets = [o for o, _ in tl.at(0.2).ticker]
        assert len(set(offsets)) == len(offsets)
        assert all(
            b - a >= cv.HUD_TICKER_CELL_COLS for a, b in zip(offsets, offsets[1:])
        )


class TestTickerModeGating:
    def _segs(self):
        return _cw_segs()

    def test_hidden_when_telemetry_says_not_cw(self):
        # The decoder runs blind on every segment and a strong tone in voice
        # audio can occasionally slip past gate_events; telemetry's own mode
        # is ground truth where we have it.
        state = [(0.0, 5.0, SegState(False, 144300000, "SSB", None))]
        assert _ticker_at(1.0, self._segs(), state) == ""

    def test_shown_when_telemetry_says_cw(self):
        state = [(0.0, 5.0, SegState(False, 144174000, "CW", None))]
        assert _ticker_at(1.0, self._segs(), state) == "HI"

    def test_shown_when_mode_is_unknown(self):
        # No positive evidence it is *not* CW -- e.g. no --telemetry at all --
        # so keep the decode rather than suppressing it.
        assert _ticker_at(1.0, self._segs(), None) == "HI"


class TestMatrixDisplay:
    def _render(self, cells, chars=4):
        img = Image.new("RGB", (chars * 24, 40), (0, 0, 0))
        cv._draw_matrix_text(
            ImageDraw.Draw(img), cells, (0, 0, chars * 24, 40), cv.HUD_GREEN, chars
        )
        return np.asarray(img)[:, :, 1]

    def test_unlit_dots_are_still_drawn_so_an_idle_display_reads_as_one(self):
        green = self._render([])
        assert green.max() > 0  # the dot grid is there
        assert green.max() < cv.HUD_GREEN[1]  # but nothing is lit

    def test_a_lit_glyph_reaches_full_brightness(self):
        assert self._render([(0, "8")]).max() == cv.HUD_GREEN[1]

    def test_a_character_scrolling_off_the_edge_is_clipped_not_wrapped(self):
        # Partly past the left edge: some columns drawn, nothing appearing on
        # the far right.
        green = self._render([(-2, "8")])
        assert green[:, -10:].max() < cv.HUD_GREEN[1]
        assert green.max() == cv.HUD_GREEN[1]


class TestSideStreamTrimming:
    def _cast(self, tmp_path, duration):
        f = tmp_path / "s.cast"
        lines = [json.dumps({"version": 2, "width": 20, "height": 4, "timestamp": 0})]
        t = 0.0
        while t <= duration:
            lines.append(json.dumps([t, "o", "x"]))
            t += 1.0
        f.write_text("\n".join(lines) + "\n")
        return str(f)

    def test_cast_render_stops_at_the_cut_instead_of_replaying_it_all(
        self, tmp_path, monkeypatch
    ):
        # A --duration preview shows the first minutes of a session, so
        # replaying the whole cast is wasted work -- and it is the slowest
        # stage, so it dominates exactly the case the flag exists to speed up.
        frames = []

        class FakeProc:
            stdin = type(
                "S",
                (),
                {"write": lambda _, b: frames.append(b), "close": lambda _: None},
            )()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: FakeProc())
        cast = self._cast(tmp_path, 60.0)
        cv.render_cast_video(cast, str(tmp_path / "o.mp4"), fps=1.0)
        full = len(frames)
        frames.clear()
        cv.render_cast_video(cast, str(tmp_path / "o.mp4"), fps=1.0, max_duration=10.0)
        assert len(frames) < full / 4
        assert len(frames) == 11  # 0..10s inclusive at 1 fps

    def test_no_limit_still_renders_the_whole_cast(self, tmp_path, monkeypatch):
        frames = []

        class FakeProc:
            stdin = type(
                "S",
                (),
                {"write": lambda _, b: frames.append(b), "close": lambda _: None},
            )()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: FakeProc())
        cv.render_cast_video(
            self._cast(tmp_path, 30.0), str(tmp_path / "o.mp4"), fps=1.0
        )
        assert len(frames) == 31


class TestRenderInputIndices:
    def _sources(self, **kw):
        """For each branch's own output label, the file that is really the
        ffmpeg input its filter chain reads from."""
        cmd = _render_cmd(**kw)
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        graph = cmd[cmd.index("-filter_complex") + 1]
        out = {}
        for label in ("scopebg", "hudbar", "castpip", "pip"):
            m = re.search(r"\[(\d+):v\]((?!\[\d+:v\]).)*?\[" + label + r"\]", graph)
            if m:
                out[label] = files[int(m.group(1))]
        return out

    def test_every_branch_reads_its_own_clip(self):
        # Regression for a real bug seen in a rendered frame: indices were
        # assigned in one order and inputs appended in another, so the HUD was
        # drawn at the cast PiP's position and size, the terminal ended up in
        # the webcam's face recess and the webcam was stretched full-width
        # along the bottom. Every branch's own filter string was well-formed,
        # so only the *combination* was wrong -- which is why this asserts
        # which file each chain reads, not merely that all four are used.
        assert self._sources(
            scope="s.mp4", scope_start=1.0, scope_end=9.0,
            cast="c.mp4", webcam="w.mp4", hud="h.mp4",
        ) == {
            "scopebg": "s.mp4",
            "hudbar": "h.mp4",
            "castpip": "c.mp4",
            "pip": "w.mp4",
        }  # fmt: skip

    def test_indices_stay_correct_with_only_some_streams(self):
        assert self._sources(cast="c.mp4", hud="h.mp4") == {
            "castpip": "c.mp4",
            "hudbar": "h.mp4",
        }
        assert self._sources(webcam="w.mp4", hud="h.mp4") == {
            "pip": "w.mp4",
            "hudbar": "h.mp4",
        }
        assert self._sources(hud="h.mp4") == {"hudbar": "h.mp4"}


class TestHudLayering:
    def _graph(self, **kw):
        cmd = _render_cmd(**kw)
        return cmd[cmd.index("-filter_complex") + 1]

    def test_the_bar_is_composited_after_the_cast_so_nothing_covers_it(self):
        # Regression from a rendered frame: the terminal PiP was drawn over
        # the bar, putting the logger's own toolbar across SCORE and QSOS.
        graph = self._graph(cast="c.mp4", hud="h.mp4")
        assert graph.index("[castpip]overlay") < graph.index("[hudbar]overlay")

    def test_the_webcam_is_composited_after_the_bar_to_sit_in_the_recess(self):
        graph = self._graph(webcam="w.mp4", hud="h.mp4")
        assert graph.index("[hudbar]overlay") < graph.index("[pip]overlay")

    def test_the_cast_is_sized_to_the_room_above_the_bar(self):
        # Height-constrained with a HUD: a width fraction picked before the
        # bar existed overran the space and had to be clipped by the bar.
        H = 1080
        expect = H - cv.hud_height(H) - 2 * round(H * cv.CAST_PIP_MARGIN_FRAC)
        assert f"scale=-2:{expect}" in self._graph(cast="c.mp4", hud="h.mp4")

    def test_without_a_hud_the_cast_keeps_its_width_based_size(self):
        graph = self._graph(cast="c.mp4")
        assert f"scale={round(1920 * cv.CAST_PIP_WIDTH_FRAC)}:-2" in graph


class TestHudTheme:
    def _theme(self, tmp_path, **over):
        art = tmp_path / "artwork.png"
        Image.new("RGB", (200, 100), (20, 20, 20)).save(art)
        theme = {
            "artwork": "artwork.png",
            "bar": [0, 0, 200, 60],
            "slots": {"score": [5, 5, 40, 30]},
            "chips": {"band": [[50, 5, 10, 10], [62, 5, 10, 10]]},
            "stats": [[100, 5, 30, 10]],
            "sprites": {"needle": {"box": [150, 60, 20, 30], "pivot": [160, 85]}},
        }
        theme.update(over)
        (tmp_path / "theme.json").write_text(json.dumps(theme))
        return str(tmp_path)

    def test_load_reads_the_json_and_its_artwork(self, tmp_path):
        theme = cv.load_hud_theme(self._theme(tmp_path))
        assert theme["image"].size == (200, 100)
        assert theme["slots"]["score"] == [5, 5, 40, 30]

    def test_every_positioned_rect_is_flattened_for_the_overlay(self, tmp_path):
        # One score slot, two chips, one stats row, one sprite.
        names = [
            n
            for n, _, _ in cv.hud_theme_rects(cv.load_hud_theme(self._theme(tmp_path)))
        ]
        assert names == [
            "slots.score", "chips.band[0]", "chips.band[1]",
            "stats[0]", "sprites.needle",
        ]  # fmt: skip

    def test_overlay_marks_every_rect_and_the_needle_pivot(self, tmp_path):
        theme = cv.load_hud_theme(self._theme(tmp_path))
        a = np.asarray(cv.hud_theme_overlay(theme))
        # cyan slot outline, orange chip, yellow stats row, green sprite, red pivot
        for colour in ((0, 255, 255), (255, 140, 0), (255, 255, 0), (0, 255, 0)):
            assert (a == np.array(colour)).all(axis=2).any(), colour
        assert a[:, :, 0].max() == 255

    def test_a_theme_without_sprites_or_chips_still_renders(self, tmp_path):
        # A hand-edited theme mid-edit may be missing whole groups; the check
        # tool has to survive that or it is useless exactly when needed.
        path = self._theme(tmp_path, chips={}, stats=[], sprites={})
        assert cv.hud_theme_overlay(cv.load_hud_theme(path)).size == (200, 100)
