"""Tests for the CW decoder and its trust gate.

No ffmpeg is invoked; the decoder is exercised against a synthesized CW WAV so
the test is fully reproducible (fixed WPM, pitch, sample rate)."""

import wave
from datetime import datetime

import numpy as np

import contest_video as cv
import cw_decode
from cw_decode import (
    MAX_OVER_S,
    CharEvent,
    _dominance,
    _quality,
    cw_subranges,
    decode_cw_subranges,
    decode_segment,
    gate_events,
)
from rig_state import (
    SegState,
)
from timeline import (
    GAP_KEEP_S,
    Segment,
    remap_audio_t,
)

SR = 16000


PITCH = 600.0


_MORSE_INV = {v: k for k, v in cw_decode.MORSE.items()}


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


class TestLongSegmentCwRecovery:
    """decode_cw_subranges recovers CW content from a segment too long to
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

    def test_decode_cw_subranges_recovers_cw_from_a_too_long_segment(self, tmp_path):
        # Regression test for a real reported case: two other stations
        # negotiate a CW frequency over voice, work each other in CW, then
        # move on -- all while we just listened, so our own recorder never
        # split the file and the whole thing became one segment far longer
        # than MAX_OVER_S. decode_segment alone never even attempts to
        # decode any of it; decode_cw_subranges recovers the CW portion
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
        spans = decode_cw_subranges(seg, state_events, PITCH)
        assert len(spans) == 1
        t0, t1, events = spans[0]
        assert abs(t0 - cw_start) < 0.01
        assert "".join(e.ch for e in events).strip() == text

    def test_decode_cw_subranges_ignores_non_cw_subranges(self, tmp_path):
        p = str(tmp_path / "20260706_163045A.wav")
        total_dur = MAX_OVER_S * 3
        _write_long_wav_with_cw_window(p, total_dur, MAX_OVER_S * 1.2, "HG7F")
        seg = Segment(p, datetime(2026, 7, 6, 16, 30, 45), total_dur, 0.0)
        # same audio, but telemetry says this whole span was SSB, not CW --
        # nothing should be extracted or decoded
        state_events = [(0.0, total_dur, SegState(mode="SSB"))]
        assert decode_cw_subranges(seg, state_events, PITCH) == []

    def test_remap_audio_t_preserves_a_long_segment_with_recovered_cw(self):
        # Without the exemption, --skip-gaps' outpoint trimming in
        # concat_audio would cut the very audio decode_cw_subranges just
        # recovered text from out of the rendered output entirely.
        long_seg = Segment(
            "long.wav", datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 50, 0.0
        )
        remap_audio_t([long_seg], cw_span_segs={id(long_seg)})
        assert long_seg.eff_dur is None

        other = Segment(
            "other.wav", datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 50, 0.0
        )
        remap_audio_t([other])
        assert other.eff_dur == GAP_KEEP_S
