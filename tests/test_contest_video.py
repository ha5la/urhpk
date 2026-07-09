"""Tests for contest_video pure logic and the CW decoder.

No ffmpeg is invoked; the decoder is exercised against a synthesized CW WAV so
the test is fully reproducible (fixed WPM, pitch, sample rate)."""
import struct
import wave
from datetime import datetime

import numpy as np

import contest_video as cv
from contest_video import (
    CAPTION_DUR_S,
    GAP_KEEP_S,
    MAX_OVER_S,
    CharEvent,
    InputLogEvent,
    Qso,
    Segment,
    SegState,
    TelemetrySample,
    _dominance,
    _eff,
    _find_offset_correction,
    _quality,
    _rms_envelope,
    _srt_time,
    _utc_at,
    _wrap,
    _yt_time,
    audio_time_for,
    build_ass,
    build_chapters,
    build_input_events,
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
    parse_edi,
    parse_wav_title,
    parse_webcam_wall,
    qso_windows,
    read_wav_metadata,
    refine_webcam_start,
    remap_audio_t,
    running_score,
    sync_webcam_start,
    trim_to_duration,
)

SR = 16000
PITCH = 600.0

_MORSE_INV = {v: k for k, v in cv.MORSE.items()}


def _write_wav_with_title(path: str, title: str) -> None:
    """A minimal WAV file carrying an IC-9700-style LIST/INFO/INAM title tag,
    for testing read_wav_metadata without needing a real recording."""
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 100)
    raw = title.encode('ascii') + b'\x00'
    pad = b'\x00' if len(raw) % 2 else b''
    inam = b'INAM' + struct.pack('<I', len(raw)) + raw + pad
    list_data = b'INFO' + inam
    list_pad = b'\x00' if len(list_data) % 2 else b''
    list_chunk = b'LIST' + struct.pack('<I', len(list_data)) + list_data + list_pad
    data = bytearray(open(path, 'rb').read())
    data.extend(list_chunk)
    data[4:8] = struct.pack('<I', len(data) - 8)
    with open(path, 'wb') as f:
        f.write(data)


def _write_cw(path: str, text: str, wpm: int = 24, amp: float = 8000.0,
              noise: float = 0.0) -> None:
    """Render `text` as Morse into a 16 kHz mono WAV at `path`."""
    unit = 1.2 / wpm  # seconds per dit
    # standard timing: dit 1u, dah 3u, symbol gap 1u, char gap 3u, word gap 7u
    on: list[tuple[bool, float]] = []
    for wi, word in enumerate(text.split(' ')):
        if wi:
            on.append((False, 7 * unit))          # word gap
        for ci, ch in enumerate(word):
            if ci:
                on.append((False, 3 * unit))      # char gap
            for si, sym in enumerate(_MORSE_INV[ch]):
                if si:
                    on.append((False, unit))      # symbol gap
                on.append((True, unit if sym == '.' else 3 * unit))
    on.append((False, 3 * unit))                  # trailing silence

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
    w = wave.open(path, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(sig.astype(np.int16).tobytes())
    w.close()


def _write_long_wav_with_cw_window(path: str, total_dur: float, cw_start: float,
                                   text: str, wpm: int = 20, pitch: float = PITCH,
                                   amp: float = 8000.0) -> tuple[float, float]:
    """Write a `total_dur`-second WAV that's silent except for `text` keyed
    as CW starting at `cw_start` -- simulating a segment far longer than
    MAX_OVER_S (e.g. listening to two other stations for several minutes)
    that still contains one real, decodable CW exchange somewhere inside it.
    Returns the CW window's own (start, end) in seconds."""
    unit = 1.2 / wpm
    on: list[tuple[bool, float]] = []
    for wi, word in enumerate(text.split(' ')):
        if wi:
            on.append((False, 7 * unit))
        for ci, ch in enumerate(word):
            if ci:
                on.append((False, 3 * unit))
            for si, sym in enumerate(_MORSE_INV[ch]):
                if si:
                    on.append((False, unit))
                on.append((True, unit if sym == '.' else 3 * unit))
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
    sig[i0:i0 + n_fit] = cw[:n_fit]

    w = wave.open(path, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(sig.astype(np.int16).tobytes())
    w.close()
    return cw_start, cw_start + len(cw) / SR


class TestDecoder:
    def test_decodes_clean_callsign_exchange(self, tmp_path):
        p = str(tmp_path / '20260704_120000A.wav')
        _write_cw(p, 'HG7F DE HA5LA 5NN TT1 JN97MM', wpm=24)
        events, snr = decode_segment(p, PITCH)
        text = ''.join(e.ch for e in events)
        assert text.replace(' ', '') == 'HG7FDEHA5LA5NNTT1JN97MM'
        assert snr > 20

    def test_character_timestamps_increase(self, tmp_path):
        p = str(tmp_path / '20260704_120000A.wav')
        _write_cw(p, 'CQ TEST', wpm=20)
        events, _ = decode_segment(p, PITCH)
        times = [e.t for e in events]
        assert times == sorted(times)
        assert times[0] >= 0.0

    def test_silence_yields_no_events(self, tmp_path):
        p = str(tmp_path / '20260704_120000A.wav')
        w = wave.open(p, 'wb')
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
        text = 'CQ TEST DE HA5LA'
        expected = text.replace(' ', '')
        for wpm in (12, 18, 24, 30, 35, 45):
            p = str(tmp_path / f'20260704_120000A_{wpm}.wav')
            _write_cw(p, text, wpm=wpm)
            events, _ = decode_segment(p, PITCH)
            decoded = ''.join(e.ch for e in events).replace(' ', '')
            assert decoded == expected, f"wpm={wpm}: got {decoded!r}"


class TestDecoderRobustness:
    @staticmethod
    def _cw_tone(text, wpm, pitch, amp, phase0=0.0):
        unit = 1.2 / wpm
        on: list[tuple[bool, float]] = []
        for wi, word in enumerate(text.split(' ')):
            if wi:
                on.append((False, 7 * unit))
            for ci, ch in enumerate(word):
                if ci:
                    on.append((False, 3 * unit))
                for si, sym in enumerate(_MORSE_INV[ch]):
                    if si:
                        on.append((False, unit))
                    on.append((True, unit if sym == '.' else 3 * unit))
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
        wanted = self._cw_tone('HG7F DE HA5LA 5NN TT1 JN97MM', 24, PITCH, 8000.0)
        interf = self._cw_tone('CQ CQ DE HG1Z HG1Z TEST CQ CQ DE HG1Z TEST', 28,
                               PITCH + 150, 6000.0, phase0=137)
        sig = wanted + np.resize(interf, len(wanted))
        p = str(tmp_path / '20260704_120000A.wav')
        w = wave.open(p, 'wb')
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(sig.astype(np.int16).tobytes())
        w.close()
        _, snr = decode_segment(p, PITCH)
        assert snr > 16.0

    def test_decodes_correctly_when_actual_tone_is_far_from_assumed_pitch(self, tmp_path):
        # Regression test for a real reported bug, much more severe than the
        # small WAV/telemetry frequency disagreement found earlier: a real
        # received-signal segment's actual tone was ~1296 Hz against the
        # assumed 600 Hz -- a 695 Hz gap entirely outside the envelope
        # lowpass's passband (LOWPASS_CUTOFF_HZ=120), so almost none of the
        # real signal survived demodulation at the wrong frequency at all.
        # decode_segment must auto-detect the real tone per segment rather
        # than trusting a single assumed pitch for the whole session.
        tone = self._cw_tone('HG7F DE HA5LA', 20, 1300.0, 8000.0)
        p = str(tmp_path / '20260704_120000A.wav')
        w = wave.open(p, 'wb')
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(tone.astype(np.int16).tobytes())
        w.close()
        events, _ = decode_segment(p, 600.0)  # deliberately wrong nominal pitch
        assert ''.join(e.ch for e in events).strip() == 'HG7F DE HA5LA'

    @staticmethod
    def _cw_tone_with_dah_glitches(text, wpm, pitch, amp, glitch_frac=0.3):
        """Like _cw_tone, but splits every dah with a brief spurious dropout
        in the middle -- simulating the near-threshold chatter a real
        received signal has that the operator's own clean TX sidetone
        doesn't (see DEBOUNCE_DIT_FRAC)."""
        unit = 1.2 / wpm
        on: list[tuple[bool, float]] = []
        for wi, word in enumerate(text.split(' ')):
            if wi:
                on.append((False, 7 * unit))
            for ci, ch in enumerate(word):
                if ci:
                    on.append((False, 3 * unit))
                for si, sym in enumerate(_MORSE_INV[ch]):
                    if si:
                        on.append((False, unit))
                    dur = unit if sym == '.' else 3 * unit
                    if sym == '-':
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

    def test_debounce_recovers_text_fragmented_by_near_threshold_chatter(self, tmp_path):
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
        text = 'HG7F DE HA5LA 5NN TT1 JN97MM'
        sig = self._cw_tone_with_dah_glitches(text, 20, PITCH, 8000.0, glitch_frac=0.3)
        p = str(tmp_path / '20260704_120000A.wav')
        w = wave.open(p, 'wb')
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(sig.astype(np.int16).tobytes())
        w.close()
        events, _ = decode_segment(p, PITCH)
        assert ''.join(e.ch for e in events).strip() == text

    def test_long_segment_is_skipped_without_decoding(self, tmp_path):
        # Segments longer than MAX_OVER_S always fail gate_events on duration
        # alone, so decode_segment should short-circuit rather than run the
        # full envelope/threshold pipeline over what can be minutes of audio.
        p = str(tmp_path / '20260704_120000A.wav')
        w = wave.open(p, 'wb')
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
        assert _quality('HG7F DE HA5LA') == 1.0
        assert _quality('E T I S E') == 0.0
        assert _quality('') == 0.0

    def test_dominance_flags_chopped_carrier(self):
        assert _dominance('TTTTTTTT') == 1.0
        assert _dominance('HG7F DE HA5LA') < 0.4

    def test_real_over_passes_gate(self):
        ev = [CharEvent(0.1 * i, c)
              for i, c in enumerate('HA5LA DE HG7F')]
        assert gate_events(15.0, ev, snr=40.0) == ev

    def test_long_noisy_segment_rejected(self):
        ev = [CharEvent(0.1 * i, c) for i, c in enumerate('E T E T I E S')]
        assert gate_events(474.0, ev, snr=25.0) == []       # too long
        assert gate_events(10.0, ev, snr=25.0) == []        # low quality

    def test_chopped_carrier_rejected(self):
        ev = [CharEvent(0.1 * i, 'T') for i in range(40)]
        assert gate_events(12.0, ev, snr=30.0) == []

    def test_short_valid_words_are_not_falsely_dominance_rejected(self):
        # Regression test for a real reported bug: a correctly-decoded "TU"
        # and "73 EE" were silently dropped from the ticker. Any 2-character
        # decode has dominance >= 0.5 by construction -- the two characters
        # either match (1.0) or don't (exactly 1/2), never less -- so
        # MAX_DOMINANCE=0.4 was structurally impossible to pass for *any*
        # two-letter contest word ("TU", "R", "K"...), independent of content.
        assert _dominance('TU') == 0.0
        assert _dominance('73EE') == 0.0
        # the pattern this check actually guards against still gets caught
        # once there's enough text for "chopped carrier" to show at all
        assert _dominance('TTTTTTTT') == 1.0


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
            captured['cmd'] = cmd

        monkeypatch.setattr(cv.subprocess, 'run', fake_run)
        cv.render(str(tmp_path / 'a.wav'), str(tmp_path / 'a.ass'),
                 str(tmp_path / 'out.mp4'), 1920, 1080,
                 webcam=str(tmp_path / 'cam.mp4'), webcam_start=10.0)
        fchain = captured['cmd'][captured['cmd'].index('-filter_complex') + 1]
        pip_chain = fchain.split('[1:v]')[1].split('[pip]')[0]
        assert f'fps={cv.RENDER_FPS}' in pip_chain

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
            captured['cmd'] = cmd

        monkeypatch.setattr(cv.subprocess, 'run', fake_run)
        cv.render(str(tmp_path / 'a.wav'), str(tmp_path / 'a.ass'),
                 str(tmp_path / 'out.mp4'), 1920, 1080,
                 webcam=str(tmp_path / 'cam.mp4'), webcam_start=10.0,
                 webcam_rate=0.0005)
        fchain = captured['cmd'][captured['cmd'].index('-filter_complex') + 1]
        pip_chain = fchain.split('[1:v]')[1].split('[pip]')[0]
        assert pip_chain.startswith('setpts=PTS/0.9995')
        assert pip_chain.index('setpts=') < pip_chain.index(f'fps={cv.RENDER_FPS}')


class TestLongSegmentCwRecovery:
    """decode_long_segment recovers CW content from a segment too long to
    decode as a whole (see MAX_OVER_S) -- e.g. two other stations
    negotiating a CW frequency over voice, working each other in CW, then
    moving on, all while we just listened without ever transmitting
    ourselves, so our own recorder never split the file."""

    def test_gate_events_check_duration_false_bypasses_length_but_not_quality(self):
        ev = [CharEvent(0.1 * i, c) for i, c in enumerate('HA5LA DE HG7F')]
        # a real over this long is normally rejected outright ...
        assert gate_events(474.0, ev, snr=40.0) == []
        # ... but not once duration is confirmed genuine by other means
        # (telemetry mode confirmation, for a sub-range extracted from a
        # longer segment)
        assert gate_events(474.0, ev, snr=40.0, check_duration=False) == ev
        # SNR/quality/dominance still apply regardless
        noisy = [CharEvent(0.1 * i, 'T') for i in range(40)]
        assert gate_events(474.0, noisy, snr=40.0, check_duration=False) == []

    def test_cw_subranges_extracts_only_cw_windows_within_segment_span(self):
        seg = Segment('a', datetime(2026, 7, 6, 16, 30, 45), 300.0, 1000.0)
        state_events = [
            (900.0, 1010.0, SegState(mode='FM')),    # starts before seg -- FM, ignored
            (1010.0, 1080.0, SegState(mode='CW')),   # fully inside -- CW, kept
            (1080.0, 1200.0, SegState(mode='SSB')),  # inside -- SSB, ignored
            (1200.0, 1400.0, SegState(mode='CW')),   # ends after seg -- CW, clipped
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
        p = str(tmp_path / '20260706_163045A.wav')
        total_dur = MAX_OVER_S * 3
        text = 'HG7F DE HA5LA'
        cw_start = MAX_OVER_S * 1.2
        _, cw_end = _write_long_wav_with_cw_window(p, total_dur, cw_start, text)
        seg = Segment(p, datetime(2026, 7, 6, 16, 30, 45), total_dur, 0.0)
        assert seg.dur > MAX_OVER_S

        # whole-file decode never even attempts it
        events, snr = decode_segment(p, PITCH)
        assert events == [] and snr == 0.0

        state_events = [(cw_start, cw_end, SegState(mode='CW'))]
        spans = decode_long_segment(seg, state_events, PITCH)
        assert len(spans) == 1
        t0, t1, events = spans[0]
        assert abs(t0 - cw_start) < 0.01
        assert ''.join(e.ch for e in events).strip() == text

    def test_decode_long_segment_ignores_non_cw_subranges(self, tmp_path):
        p = str(tmp_path / '20260706_163045A.wav')
        total_dur = MAX_OVER_S * 3
        _write_long_wav_with_cw_window(p, total_dur, MAX_OVER_S * 1.2, 'HG7F')
        seg = Segment(p, datetime(2026, 7, 6, 16, 30, 45), total_dur, 0.0)
        # same audio, but telemetry says this whole span was SSB, not CW --
        # nothing should be extracted or decoded
        state_events = [(0.0, total_dur, SegState(mode='SSB'))]
        assert decode_long_segment(seg, state_events, PITCH) == []

    def test_remap_audio_t_preserves_a_long_segment_with_recovered_cw(self):
        # Without the exemption, --skip-gaps' outpoint trimming in
        # concat_audio would cut the very audio decode_long_segment just
        # recovered text from out of the rendered output entirely.
        long_seg = Segment('long.wav', datetime(2026, 7, 4, 13, 0, 0),
                           MAX_OVER_S + 50, 0.0)
        remap_audio_t([long_seg], long_cw_segs={id(long_seg)})
        assert long_seg.eff_dur is None

        other = Segment('other.wav', datetime(2026, 7, 4, 13, 0, 0),
                        MAX_OVER_S + 50, 0.0)
        remap_audio_t([other])
        assert other.eff_dur == GAP_KEEP_S

    def test_ticker_treats_disjoint_long_cw_spans_as_separate_bursts(self, tmp_path):
        # Two CW exchanges recovered from within the *same* long segment,
        # ~150s apart -- more than a genuine gap (MAX_OVER_S) -- must not
        # be shown as one continuous, un-flushed transcript: they're
        # unrelated exchanges we happened to follow one after the other.
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        long_seg = Segment('a', datetime(2026, 7, 4, 13, 0, 0), 300.0, 0.0)
        long_cw_spans = [
            (30.0, 85.0, [CharEvent(0.0, 'A'), CharEvent(1.0, 'B')]),
            (203.0, 260.0, [CharEvent(0.0, 'X'), CharEvent(1.0, 'Y')]),
        ]
        ass = build_ass([long_seg], qsos, mycall, mywwl, 'TEST', 0, 1920, 1080,
                        long_cw_spans=long_cw_spans)
        texts = [line.rsplit(',', 1)[-1] for line in ass.splitlines()
                if line.startswith('Dialogue:') and ',Ticker,' in line]
        seen_x = False
        for text in texts:
            if 'X' in text:
                seen_x = True
            if seen_x:
                assert 'A' not in text and 'B' not in text, \
                    f"first exchange leaked into the second: {text!r}"
        assert seen_x, "second exchange's characters never reached the ticker"


class TestEdi:
    def test_parse_edi(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "[REG1TEST;1]\n"
            "PCall=HA5LA\n"
            "PWWLo=JN97MM\n"
            "[QSORecords;2]\n"
            "260704;0908;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
            "260704;0929;HA7NK;2;599;004;599;029;;JN97WW;0;;;D;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        assert (mycall, mywwl) == ('HA5LA', 'JN97MM')
        assert len(qsos) == 2
        assert qsos[0].call == 'HG7F' and qsos[0].pts == 26
        assert qsos[0].dt == datetime(2026, 7, 4, 9, 8)
        assert qsos[1].dup is True and qsos[1].pts == 0

    def test_merge_edi_combines_and_sorts_multiple_bands(self, tmp_path):
        # A session worked on two bands writes two EDI files -- one physical
        # recording still needs a single chronological QSO list.
        band_2m = tmp_path / '2m.edi'
        band_2m.write_text(
            "PCall=HA5LA\nPWWLo=JN97TF\n[QSORecords;2]\n"
            "260706;1601;A;1;59;001;59;001;;JN86SR;167;;;;\n"
            "260706;1720;C;1;59;003;59;003;;JN86SR;167;;;;\n"
        )
        band_70cm = tmp_path / '70cm.edi'
        band_70cm.write_text(
            "PCall=HA5LA\nPWWLo=JN97TF\n[QSORecords;1]\n"
            "260706;1615;B;1;59;001;59;002;;JN97WM;37;;;;\n"
        )
        mycall, mywwl, qsos = merge_edi([str(band_2m), str(band_70cm)])
        assert (mycall, mywwl) == ('HA5LA', 'JN97TF')
        assert [q.call for q in qsos] == ['A', 'B', 'C']  # chronological, bands interleaved


class TestTrimToDuration:
    def _segs(self):
        return [
            Segment('a', datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment('b', datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
            Segment('c', datetime(2026, 7, 4, 11, 2, 0), 60.0, 120.0),
        ]

    def test_drops_segments_past_the_cutoff(self):
        out = trim_to_duration(self._segs(), 90.0)
        assert [s.path for s in out] == ['a', 'b']

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
        assert parse_webcam_wall('VID_20260706_180003.mp4') == \
            datetime(2026, 7, 6, 18, 0, 3)

    def test_sync_derives_the_cams_own_offset_not_the_recorders(self):
        # Main WAV recorder's own convention: wall = UTC+2 (mirrors
        # TestTimeline.test_derive_utc_offset).
        segs = [
            Segment('a', datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment('b', datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
        ]
        qsos = [
            Qso(datetime(2026, 7, 4, 9, 0), 'A', '599', '1', '599', '2',
                'JN97MM', 10, False),
            Qso(datetime(2026, 7, 4, 9, 2), 'B', '599', '3', '599', '4',
                'JN97MM', 10, False),
        ]
        offset_h = derive_utc_offset(segs, qsos)
        assert offset_h == 2

        # The phone uses a *different* clock convention (UTC+5, not +2) --
        # its filename wall-clock is 14:00:00, real recording start is UTC
        # 09:00:00, which is exactly the start of the session.
        cam_wall = datetime(2026, 7, 4, 14, 0, 0)
        start = sync_webcam_start(cam_wall, cam_dur=120.0, qsos=qsos,
                                  segs=segs, offset_h=offset_h)
        assert start == 0.0

    def test_sync_clamps_to_session_start_when_cam_starts_earlier(self):
        segs = [Segment('a', datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0)]
        qsos = [Qso(datetime(2026, 7, 4, 9, 0), 'A', '599', '1', '599', '2',
                     'JN97MM', 10, False)]
        # cam wall-clock a full day earlier than the session -- however its
        # own offset resolves, the real recording predates segs[0], so the
        # result clamps to the session's own start rather than going negative.
        cam_wall = datetime(2026, 7, 3, 8, 0, 0)
        start = sync_webcam_start(cam_wall, cam_dur=30.0, qsos=qsos,
                                  segs=segs, offset_h=2)
        assert start == 0.0


def _burst_signal(sr: int, total_dur: float, burst_starts: float | list[float],
                  burst_dur: float = 0.15, amp: float = 1000.0, seed: int = 0) -> np.ndarray:
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
        segs = [Segment(f'seg{i}.wav', datetime(2026, 7, 4, 13, 0, 0), radio_dur, t,
                        ptt=True)
                for i, t in enumerate(audio_ts)]

        def fake_read_wav_range(path, t0, t1):
            return _burst_signal(sr, radio_dur, radio_bursts, seed=1), sr

        def fake_read_webcam_audio_range(webcam_path, src_start, dur, sr=16000):
            seg_audio_t = src_start + webcam_start_coarse + padding_s
            true_correction = true_intercept + true_rate * seg_audio_t
            cam_bursts = [padding_s - true_correction + b for b in radio_bursts]
            return _burst_signal(sr, dur, cam_bursts, seed=1), sr

        monkeypatch.setattr(cv, '_read_wav_range', fake_read_wav_range)
        monkeypatch.setattr(cv, '_read_webcam_audio_range', fake_read_webcam_audio_range)

        refined, rate, n = refine_webcam_start('fake_cam.mp4', segs, webcam_start_coarse,
                                               max_anchors=20, padding_s=padding_s)
        assert n == len(segs)
        assert abs(rate - true_rate) < 0.0001
        assert abs((refined - webcam_start_coarse) - true_intercept) < 0.2

    def test_refine_webcam_start_unchanged_with_no_confident_anchors(self, monkeypatch):
        segs = [Segment('a.wav', datetime(2026, 7, 4, 13, 0, 0), 3.0, 100.0, ptt=True)]

        def fake_read_wav_range(path, t0, t1):
            return _burst_signal(16000, 3.0, [1.0, 1.4, 1.9, 2.3], seed=1), 16000

        def fake_read_webcam_audio_range(webcam_path, src_start, dur, sr=16000):
            return _silence_signal(sr, dur), sr  # no matching speech at all

        monkeypatch.setattr(cv, '_read_wav_range', fake_read_wav_range)
        monkeypatch.setattr(cv, '_read_webcam_audio_range', fake_read_webcam_audio_range)

        refined, rate, n = refine_webcam_start('fake_cam.mp4', segs, 100.0)
        assert (refined, rate, n) == (100.0, 0.0, 0)


class TestSkipGaps:
    def _segs_with_gap(self):
        # short over (15 s, has events) then long gap (500 s, no events)
        return [
            Segment('a', datetime(2026, 7, 4, 11, 0, 0), 15.0, 0.0,
                    events=[CharEvent(1.0, 'H')]),
            Segment('b', datetime(2026, 7, 4, 11, 0, 15), 500.0, 15.0),
        ]

    def test_eff_defaults_to_dur(self):
        s = Segment('x', datetime(2026, 7, 4, 11, 0), 42.0, 0.0)
        assert _eff(s) == 42.0

    def test_remap_shortens_gap_segments(self):
        segs = self._segs_with_gap()
        remap_audio_t(segs)
        assert segs[0].eff_dur is None          # short over: unchanged
        assert segs[1].eff_dur == GAP_KEEP_S    # long gap: trimmed
        assert _eff(segs[0]) == 15.0
        assert _eff(segs[1]) == GAP_KEEP_S

    def test_remap_recomputes_audio_t(self):
        segs = self._segs_with_gap()
        remap_audio_t(segs)
        assert segs[0].audio_t == 0.0
        assert segs[1].audio_t == 15.0          # immediately after the short over

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
            Segment('a', datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment('b', datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
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
            cv.Qso(datetime(2026, 7, 4, 9, 0), 'A', '599', '1', '599', '2',
                   'JN97MM', 10, False),
            cv.Qso(datetime(2026, 7, 4, 9, 2), 'B', '599', '3', '599', '4',
                   'JN97MM', 10, False),
        ]
        assert derive_utc_offset(segs, qsos) == 2


class TestUtcAt:
    def _segs(self):
        return [
            Segment('a', datetime(2026, 7, 4, 11, 0, 0), 60.0, 0.0),
            Segment('b', datetime(2026, 7, 4, 11, 1, 0), 60.0, 60.0),
        ]

    def test_maps_video_time_to_utc(self):
        segs = self._segs()
        utc = _utc_at(30.0, segs, offset_h=2)
        assert utc == datetime(2026, 7, 4, 9, 0, 30)

    def test_returns_none_past_end(self):
        segs = self._segs()
        assert _utc_at(9999.0, segs, offset_h=2) is None

    def test_clock_in_ass(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 11, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080)
        assert '2026-07-04 09:00:00Z' in ass
        assert 'Style: Clock' in ass


class TestAss:
    def test_wrap_keeps_last_lines(self):
        wrapped = _wrap('AAAA BBBB CCCC DDDD EEEE', cpl=9, keep=2)
        assert wrapped.count('\\N') == 1          # exactly two lines
        assert wrapped.endswith('EEEE')

    def test_build_ass_has_events_and_resolution(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1100;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0,
                        events=[CharEvent(1.0, 'H'), CharEvent(1.5, 'I')])]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080)
        assert 'PlayResX: 1920' in ass
        assert 'Dialogue:' in ass
        assert 'HG7F' in ass

    def _ticker_texts(self, ass: str) -> list[str]:
        texts = []
        for line in ass.splitlines():
            if line.startswith('Dialogue:') and ',Ticker,' in line:
                texts.append(line.rsplit(',', 1)[-1])
        return texts

    def test_ticker_does_not_leak_across_a_genuine_gap(self, tmp_path):
        # Regression test for a real bug: the ticker used to flush at a QSO's
        # EDI-log time (minute precision only) minus a fixed lead, which could
        # land seconds *into* the next real over -- so that over's opening
        # characters got appended to the previous QSO's leftover transcript
        # instead of starting fresh. The flush must instead trigger exactly
        # at the first character of a real over that follows a genuine
        # listening gap (dur > MAX_OVER_S, no events), regardless of any QSO
        # timestamp.
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0,
                    events=[CharEvent(1.0, 'A'), CharEvent(2.0, 'B')]),          # QSO 1 tail
            Segment('b', datetime(2026, 7, 4, 13, 0, 10), 474.0, 10.0),          # real listening gap
            Segment('c', datetime(2026, 7, 4, 13, 7, 4), 5.0, 484.0,
                    events=[CharEvent(0.01, 'X'), CharEvent(0.6, 'Y')]),         # QSO 2 begins
        ]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080)
        texts = self._ticker_texts(ass)
        # every ticker event from segment c onward must be free of QSO 1's
        # leftover characters -- once 'X' (segment c's first char) appears,
        # no event may still contain 'A' or 'B'
        seen_x = False
        for text in texts:
            if 'X' in text:
                seen_x = True
            if seen_x:
                assert 'A' not in text and 'B' not in text, \
                    f"QSO 1 leftover leaked into segment c's ticker: {text!r}"
        assert seen_x, "segment c's characters never reached the ticker"

    def test_ticker_does_not_leak_across_many_short_non_cw_segments(self, tmp_path):
        # Regression test for a real bug found watching an actual rendered
        # video: at 16:42:04Z a fresh CW QSO started, but the ticker still
        # showed the tail end of a CW QSO decoded over four minutes
        # earlier. Between the two CW QSOs the operator worked several
        # SSB/FM contacts, each individually short (dur <= MAX_OVER_S) --
        # so no *single* segment in between ever looked like a "genuine
        # gap" to the old flush logic, which only checked whether the one
        # immediately-preceding segment was long. Real elapsed time across
        # all of them combined was well over four minutes. The flush
        # decision must be based on the real time gap since the last
        # *included* (CW) chunk, not on any one segment in between.
        # Verified red before green: the old per-segment `prev_was_gap`
        # logic (git 68d57c1) produces the final transcript 'AB XY' on
        # this exact data -- 'A'/'B' never flushed away before 'X'/'Y'.
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 4, 16, 37, 44), 10.0, 0.0,
                    events=[CharEvent(1.0, 'A'), CharEvent(2.0, 'B')]),  # CW QSO 1 tail
        ]
        t = 10.0
        for i in range(20):  # ~2.5 minutes of short FM/SSB overs, none > MAX_OVER_S
            segs.append(Segment(f'b{i}', datetime(2026, 7, 4, 16, 37, 54), 8.0, t))
            t += 8.0
        segs.append(Segment('c', datetime(2026, 7, 4, 16, 42, 0), 5.0, t,
                            events=[CharEvent(0.01, 'X'), CharEvent(0.6, 'Y')]))  # CW QSO 2
        state_events = [
            (0.0, 10.0, SegState(mode='CW')),
            (10.0, t, SegState(mode='FM')),
            (t, t + 5.0, SegState(mode='CW')),
        ]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 0, 1920, 1080, state_events)
        texts = self._ticker_texts(ass)
        seen_x = False
        for text in texts:
            if 'X' in text:
                seen_x = True
            if seen_x:
                assert 'A' not in text and 'B' not in text, \
                    f"CW QSO 1 leftover leaked across the short-segment stretch: {text!r}"
        assert seen_x, "CW QSO 2's characters never reached the ticker"

    def test_cluster_starts_marks_first_segment_and_after_long_gap_only(self):
        segs = [
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                    events=[CharEvent(0.0, 'A')]),                       # 1st segment: burst start
            Segment('b', datetime(2026, 7, 4, 13, 0, 5), 5.0, 5.0),      # short silence, no events
            Segment('c', datetime(2026, 7, 4, 13, 0, 10), 5.0, 10.0,
                    events=[CharEvent(0.0, 'B')]),                       # continuation (short gap before it)
            Segment('d', datetime(2026, 7, 4, 13, 0, 15), MAX_OVER_S + 1, 15.0),  # genuine gap
            Segment('e', datetime(2026, 7, 4, 13, 0, 50), 5.0, 50.0,
                    events=[CharEvent(0.0, 'C')]),                       # new burst
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
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), MAX_OVER_S + 1, 0.0),  # listening gap
            Segment('b', datetime(2026, 7, 4, 13, 0, 40), 5.0, 40.0),           # voice over, no CW events
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
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 26.11, 0.0),     # RX: listening
            Segment('b', datetime(2026, 7, 4, 13, 0, 26), 2.13, 26.11),   # TX: the real start
            Segment('c', datetime(2026, 7, 4, 13, 0, 28), 5.54, 28.24),   # RX: listening for reply
            Segment('d', datetime(2026, 7, 4, 13, 0, 34), 5.41, 33.78),   # TX: continuing
        ]
        assert cluster_starts(segs) == [26.11]

    def test_qso_window_snaps_to_real_burst_not_edi_minute(self, tmp_path):
        # EDI only has minute precision, so audio_time_for(qso.dt) lands
        # somewhere inside the real over rather than at its start. The panel
        # window must snap to where the over actually begins.
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1117;HA7NK;2;599;002;599;014;;JN97WW;77;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                    events=[CharEvent(0.0, 'A')]),
            Segment('b', datetime(2026, 7, 4, 13, 0, 5), 474.0, 5.0),
            # real over begins here, well before the EDI's truncated :00 second
            Segment('c', datetime(2026, 7, 4, 13, 17, 47), 5.0, 479.0,
                    events=[CharEvent(0.0, 'H')]),
        ]
        offset_h = 2
        total = 484.0
        [(start, _end)] = qso_windows(qsos, segs, offset_h, total)
        assert start == 479.0   # snapped to segment c's real start, not ~486ish

    def test_qso_window_snaps_to_own_burst_not_the_next_ones(self, tmp_path):
        # Regression test for a real bug found by the user: if a QSO takes a
        # while to complete (calling, retries) before being logged, its
        # EDI-derived approximate time can end up numerically *closer* to
        # the following contact's real burst than to its own. Picking the
        # nearest cluster then wrongly snaps QSO N onto QSO N+1's burst. The
        # correct rule is the *latest* burst that started at or before the
        # approximate time, since a QSO's own over must have begun before it
        # was logged.
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1301;HA5MA;2;599;003;599;019;;JN97MK;9;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 100.0,
                    events=[CharEvent(0.0, 'X')]),               # this QSO's real burst
            Segment('b', datetime(2026, 7, 4, 13, 0, 5), 100.0, 105.0),   # genuine gap
            Segment('c', datetime(2026, 7, 4, 13, 1, 45), 5.0, 205.0,
                    events=[CharEvent(0.0, 'Y')]),               # the *next* contact's burst
        ]
        [(start, _end)] = qso_windows(qsos, segs, offset_h=0, total=210.0)
        assert start == 100.0    # not 205.0 (the next burst, numerically closer)

    def test_qso_window_before_any_cluster_uses_approx_time(self, tmp_path):
        # Regression test for a real bug found by the user on a mostly-voice
        # ("mix" mode) recording: a QSO logged before any CW was ever
        # decoded (e.g. an early SSB contact, or simply the very first QSO)
        # has no earlier cluster to snap to. Falling back to the *first*
        # cluster in the whole recording pulled the panel far into the
        # future (minutes off in the real case) instead of just using the
        # coarse EDI-derived time, which -- while not audio-precise -- is at
        # least in the right neighbourhood.
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1300;HA7NK;1;59;001;59;014;;JN97WW;77;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 4, 13, 0, 0), 300.0, 0.0),   # voice, no CW events
            Segment('b', datetime(2026, 7, 4, 13, 5, 0), 5.0, 300.0,
                    events=[CharEvent(0.0, 'Z')]),                     # first-ever CW burst
        ]
        [(start, _end)] = qso_windows(qsos, segs, offset_h=0, total=305.0)
        assert start == 0.0     # not 300.0 (the first cluster, minutes away)


class TestChaptersAndSrt:
    def test_yt_time_formats(self):
        assert _yt_time(0) == '0:00'
        assert _yt_time(65) == '1:05'
        assert _yt_time(3665) == '1:01:05'

    def test_srt_time_formats(self):
        assert _srt_time(65.5) == '00:01:05,500'

    def test_qso_windows_spans_to_next_qso(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;2]\n"
            "260704;1100;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
            "260704;1110;HA7NK;2;599;002;599;014;;JN97WW;77;;;;\n"
        )
        _, _, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0)]
        total = 1200.0
        windows = qso_windows(qsos, segs, offset_h=2, total=total)
        assert len(windows) == 2
        assert windows[0][1] == windows[1][0]   # first ends when second begins
        assert windows[1][1] == total

    def test_build_chapters_starts_at_zero(self, tmp_path):
        edi = tmp_path / 'log.edi'
        # QSO 2 min into the segment so its own chapter lands well after 0:00
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;1]\n"
            "260704;1102;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
        )
        _, _, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0)]
        windows = qso_windows(qsos, segs, offset_h=2, total=1200.0)
        chapters = build_chapters(qsos, windows)
        lines = chapters.strip().splitlines()
        assert lines[0] == '0:00 Start'
        assert 'HG7F' in chapters

    def test_build_chapters_drops_qsos_closer_than_min_gap(self):
        qsos = [
            Qso(datetime(2026, 7, 4, 11, 0, 0), 'HG7F',
                '599', '001', '599', '010', 'JN97KR', 26, False),
            Qso(datetime(2026, 7, 4, 11, 0, 5), 'HA7NK',
                '599', '002', '599', '014', 'JN97WW', 77, False),
        ]
        windows = [(60.0, 65.0), (65.0, 100.0)]
        chapters = build_chapters(qsos, windows)
        assert chapters.count('QSO') == 1   # second is only 5s after the first

    def test_build_srt_has_call_and_dupe_tag_and_capped_duration(self):
        qsos = [Qso(datetime(2026, 7, 4, 11, 0, 0), 'HG7F',
                    '599', '001', '599', '010', 'JN97KR', 0, True)]
        windows = [(10.0, 70.0)]   # far longer than CAPTION_DUR_S
        srt = build_srt(qsos, windows)
        assert f"00:00:10,000 --> 00:00:{10 + int(CAPTION_DUR_S):02d},000" in srt
        assert 'HG7F' in srt
        assert 'DUPE' in srt


class TestWavMetadata:
    def test_parse_ssb(self):
        title = ('IC-9700 Voice Recorder Data   144.299.84 USB    '
                 '----.---.-- ------ -- TX 2026-07-06 16:00:37')
        assert parse_wav_title(title) == (144299840, 'SSB', True)

    def test_parse_cw(self):
        title = ('IC-9700 Voice Recorder Data   144.080.00 CW     '
                 '----.---.-- ------ -- TX 2026-07-06 16:03:24')
        assert parse_wav_title(title) == (144080000, 'CW', True)

    def test_parse_fm_rx(self):
        title = ('IC-9700 Voice Recorder Data   145.350.00 FM     '
                 '----.---.-- ------ -- RX 2026-07-06 16:49:24')
        assert parse_wav_title(title) == (145350000, 'FM', False)

    def test_parse_lsb_normalizes_to_ssb(self):
        title = ('IC-9700 Voice Recorder Data   432.109.75 LSB    '
                 '----.---.-- ------ -- RX 2026-07-06 16:37:24')
        freq_hz, mode, ptt = parse_wav_title(title)
        assert mode == 'SSB'

    def test_parse_returns_none_for_unrecognized_format(self):
        assert parse_wav_title('not an IC-9700 title at all') is None
        assert parse_wav_title('') is None

    def test_read_wav_metadata_populates_segment(self, tmp_path):
        path = tmp_path / 'seg.wav'
        _write_wav_with_title(path, 'IC-9700 Voice Recorder Data   144.080.00 CW     '
                                     '----.---.-- ------ -- TX 2026-07-06 16:03:24')
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz == 144080000
        assert segs[0].mode == 'CW'
        assert segs[0].ptt is True

    def test_read_wav_metadata_leaves_none_without_a_tag(self, tmp_path):
        path = tmp_path / 'plain.wav'
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b'\x00\x00' * 100)
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz is None
        assert segs[0].mode is None
        assert segs[0].ptt is None


class TestTelemetryAlignment:
    def test_load_telemetry_parses_lines_and_skips_bad_ones(self, tmp_path):
        f = tmp_path / 'telem.jsonl'
        f.write_text(
            '{"t": "2026-07-04T11:00:02Z", "freq_hz": 144174000, '
            '"mode": "CW", "az": 135.0}\n'
            'not json\n'
            '{"t": "2026-07-04T11:00:05Z", "freq_hz": null}\n'
        )
        samples = load_telemetry(str(f))
        assert len(samples) == 2
        assert samples[0] == TelemetrySample(
            datetime(2026, 7, 4, 11, 0, 2), 144174000, 'CW', 135.0)
        assert samples[1].freq_hz is None

    def _wav_seg(self, wall, dur, audio_t, freq_hz, mode, ptt):
        s = Segment('a', wall, dur, audio_t)
        s.freq_hz, s.mode, s.ptt = freq_hz, mode, ptt
        return s

    def test_ptt_comes_from_wav_metadata_regardless_of_telemetry(self):
        # ptt never needs telemetry any more -- it's ground truth straight
        # from the WAV file itself (see build_state_events' docstring for
        # why: unlike freq/mode, ptt cannot legitimately change mid-segment,
        # so the WAV metadata alone is always sufficient and telemetry's own
        # up-to-1-second polling lag is no longer a concern at all).
        segs = [self._wav_seg(datetime(2026, 7, 6, 16, 0, 37), 2.214, 142.533,
                              144299840, 'SSB', True)]
        [(start, end, st)] = build_state_events(segs, [], offset_h=0)
        assert start == 142.533          # exactly the WAV segment boundary
        assert end == 142.533 + 2.214
        assert st.ptt is True
        assert st.freq_hz == 144299840
        assert st.mode == 'SSB'

    def test_wav_value_used_for_whole_segment_without_telemetry_change(self):
        segs = [self._wav_seg(datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0,
                              144174000, 'CW', True)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 2), 144174000, 'CW', 135.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 5), 144174000, 'CW', 136.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 8), 144174000, 'CW', 137.0),
        ]
        [(start, end, st)] = build_state_events(segs, telemetry, offset_h=2)
        assert (start, end) == (0.0, 10.0)
        assert st.freq_hz == 144174000
        assert st.mode == 'CW'
        assert st.az == 136.0          # median of 135/136/137

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
        segs = [self._wav_seg(datetime(2026, 7, 6, 16, 0, 37), 2.214, 142.533,
                              144299840, 'SSB', True)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 6, 16, 0, 37), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 16, 0, 38), 144300000, 'SSB', None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        assert len(events) == 1
        assert events[0][2].freq_hz == 144299840   # stayed on the WAV's own value

    def test_long_segment_splits_on_a_real_frequency_change(self):
        # Regression test for the original reported bug: a long idle/
        # listening segment (no PTT to split the WAV on) where the operator
        # QSY'd partway through used to get ONE majority-voted state for
        # its entire span. Real values from the July round: SSB 144.300 MHz
        # held 16:05:25-16:05:28, then a CW QSY through
        # 432.080/.088/.179/.199/.200 MHz -- each step far larger than the
        # WAV/telemetry disagreement tolerance, so still correctly detected.
        segs = [self._wav_seg(datetime(2026, 7, 6, 13, 0, 0), 11.0, 0.0,
                              144300000, 'SSB', False)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 0), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 1), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 2), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 3), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 4), 144300000, 'SSB', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 5), 432080000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 6), 432088000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 7), 432179000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 8), 432199000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 9), 432199000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 6, 13, 0, 10), 432200000, 'CW', None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        [ev] = [e for e in events if e[0] <= 6.0 < e[1]]
        assert ev[2].freq_hz == 432088000
        assert ev[2].mode == 'CW'
        assert not any(e[2].freq_hz == 144300000 and e[0] <= 6.0 < e[1] for e in events)

    def test_segment_without_wav_metadata_produces_no_event(self):
        # No WAV tag at all (freq_hz/mode/ptt all None) -- skipped rather
        # than guessed at from telemetry alone.
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        telemetry = [TelemetrySample(datetime(2026, 7, 4, 13, 0, 2), 144174000, 'CW', 135.0)]
        assert build_state_events(segs, telemetry, offset_h=0) == []

    def test_a_momentary_none_reading_does_not_split_a_run(self):
        # A single dropped rigctld poll shouldn't fragment an otherwise
        # stable state into spurious extra badge events.
        segs = [self._wav_seg(datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                              144174000, 'CW', True)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 0), 144174000, 'CW', None),
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 1), None, None, None),
            TelemetrySample(datetime(2026, 7, 4, 13, 0, 2), 144174000, 'CW', None),
        ]
        events = build_state_events(segs, telemetry, offset_h=0)
        assert len(events) == 1
        assert events[0][2].freq_hz == 144174000

    def test_build_ass_includes_state_badge_and_rig_info(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080,
                        state_events=[(0.0, 10.0, SegState(True, 144174000, 'CW', 135.0))])
        assert 'Style: State' in ass
        assert 'TX' in ass
        assert '144.174 MHz' in ass
        assert 'CW' in ass
        assert 'ROT 135' in ass

    def test_build_ass_omits_badge_when_ptt_unknown(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080,
                        state_events=[(0.0, 10.0, SegState())])
        assert ',State,' not in ass

    def test_ticker_hidden_when_telemetry_says_not_cw(self):
        # Regression test for the "hide the CW ticker outside CW" request:
        # a segment with decoded (gated-trusted) characters must not show
        # them in the ticker if telemetry confirms the rig was on SSB/FM at
        # the time -- the decoder runs blind on every segment and a strong
        # tone in voice audio can occasionally still slip past gate_events.
        edi_qsos: list[Qso] = []
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                        events=[CharEvent(0.5, 'H'), CharEvent(0.6, 'I')])]
        ass = build_ass(segs, edi_qsos, 'HA5LA', 'JN97MM', 'TEST', 0, 1920, 1080,
                        state_events=[(0.0, 5.0, SegState(False, 144300000, 'SSB', None))])
        assert 'Style: Ticker' in ass
        assert ',Ticker,' not in ass

    def test_ticker_shown_when_telemetry_says_cw(self):
        edi_qsos: list[Qso] = []
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                        events=[CharEvent(0.5, 'H'), CharEvent(0.6, 'I')])]
        ass = build_ass(segs, edi_qsos, 'HA5LA', 'JN97MM', 'TEST', 0, 1920, 1080,
                        state_events=[(0.0, 5.0, SegState(False, 144174000, 'CW', None))])
        assert ',Ticker,' in ass

    def test_ticker_shown_when_mode_unknown(self):
        # No positive evidence it's *not* CW -- keep existing behaviour
        # (e.g. no --telemetry passed at all) rather than suppressing.
        edi_qsos: list[Qso] = []
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
                        events=[CharEvent(0.5, 'H'), CharEvent(0.6, 'I')])]
        ass = build_ass(segs, edi_qsos, 'HA5LA', 'JN97MM', 'TEST', 0, 1920, 1080,
                        state_events=None)
        assert ',Ticker,' in ass


def _text(t, text):
    return InputLogEvent(t, 'text', text=text)


def _qso_ev(t, call, dup=False):
    return InputLogEvent(t, 'qso', call=call, dup=dup)


class TestInputTypewriter:
    def test_load_input_log_parses_both_event_kinds(self, tmp_path):
        f = tmp_path / 'input.jsonl'
        f.write_text(
            '{"t": "2026-07-04T11:00:02.123456Z", "event": "text", "text": "H"}\n'
            'not json\n'
            '{"t": "2026-07-04T11:00:05.000000Z", "event": "qso", "call": "HA7NS", "dup": false}\n'
        )
        log = load_input_log(str(f))
        assert log == [
            InputLogEvent(datetime(2026, 7, 4, 11, 0, 2, 123456), 'text', text='H'),
            InputLogEvent(datetime(2026, 7, 4, 11, 0, 5), 'qso', call='HA7NS', dup=False),
        ]

    def test_load_input_log_defaults_missing_event_field_to_text(self, tmp_path):
        # Written before the "event" field existed, or hand-crafted -- treat
        # as a keystroke rather than dropping it.
        f = tmp_path / 'input.jsonl'
        f.write_text('{"t": "2026-07-04T11:00:02.000000Z", "text": "H"}\n')
        log = load_input_log(str(f))
        assert log == [InputLogEvent(datetime(2026, 7, 4, 11, 0, 2), 'text', text='H')]

    def test_build_input_events_windows_between_keystrokes(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0)]
        log = [
            _text(datetime(2026, 7, 4, 11, 0, 2), 'H'),
            _text(datetime(2026, 7, 4, 11, 0, 3), 'HA'),
            _text(datetime(2026, 7, 4, 11, 0, 5), ''),   # buffer cleared
        ]
        windows = build_input_events(log, segs, offset_h=2, total=60.0)
        # offset_h=2: UTC 11:00:02 -> wall 13:00:02 -> audio_t 2.0, etc.
        assert windows == [(2.0, 3.0, 'H'), (3.0, 5.0, 'HA')]

    def test_build_input_events_ignores_qso_events(self):
        # A 'qso' event doesn't change what's on screen, so it must not
        # split or shift a keystroke window.
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0)]
        log = [
            _text(datetime(2026, 7, 4, 11, 0, 2), 'HA7NS 59 015'),
            _qso_ev(datetime(2026, 7, 4, 11, 0, 4), 'HA7NS'),
            _text(datetime(2026, 7, 4, 11, 0, 6), ''),
        ]
        windows = build_input_events(log, segs, offset_h=2, total=60.0)
        assert windows == [(2.0, 6.0, 'HA7NS 59 015')]

    def test_build_input_events_drops_empty_text(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0)]
        log = [_text(datetime(2026, 7, 4, 11, 0, 2), '')]
        assert build_input_events(log, segs, offset_h=2, total=60.0) == []

    def test_build_input_events_empty_log(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0)]
        assert build_input_events([], segs, offset_h=2, total=60.0) == []

    def test_last_keystroke_extends_to_total(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 60.0, 0.0)]
        log = [_text(datetime(2026, 7, 4, 11, 0, 2), 'HA7NS')]
        windows = build_input_events(log, segs, offset_h=2, total=60.0)
        assert windows == [(2.0, 60.0, 'HA7NS')]

    def test_build_ass_renders_typewriter_line(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080,
                        input_events=[(0.0, 5.0, 'HA7NS 59')])
        assert 'Style: Input' in ass
        assert '► HA7NS 59' in ass

    def test_build_ass_omits_input_style_without_events(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080)
        assert ',Input,' not in ass


class TestMatchQsoTimes:
    def _qso(self, dt, call):
        return Qso(dt, call, '59', '1', '59', '2', 'JN97MM', 10, False)

    def test_matches_by_call_for_a_single_occurrence(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS')]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 42, 123456), 'HA7NS')]
        [t] = match_qso_times(qsos, log)
        assert t == datetime(2026, 7, 6, 16, 1, 42, 123456)

    def test_matches_across_a_hand_edited_minute_boundary(self):
        # A seeded skeleton (--seed-input-log) starts with the EDI's own
        # minute, but the whole point is the operator then edits 't' to the
        # real time from the audio -- which can easily land in a different
        # minute than the EDI recorded (e.g. the over started well before
        # Enter was pressed). Matching must not depend on the two agreeing.
        qsos = [self._qso(datetime(2026, 7, 6, 16, 5), 'HA3KHB')]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 42), 'HA3KHB')]  # edited 4 minutes earlier
        [t] = match_qso_times(qsos, log)
        assert t == datetime(2026, 7, 6, 16, 1, 42)

    def test_none_when_no_input_log(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS')]
        assert match_qso_times(qsos, []) == [None]

    def test_none_for_unmatched_call(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS')]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 10), 'HA3KHB')]
        assert match_qso_times(qsos, log) == [None]

    def test_text_events_are_not_candidates(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS')]
        log = [_text(datetime(2026, 7, 6, 16, 1, 10), 'HA7NS 59 001')]
        assert match_qso_times(qsos, log) == [None]

    def test_repeated_call_resolved_in_encounter_order(self):
        # Same call worked twice (e.g. two different bands) -- the two
        # 'qso' events must not both map to the first QSO.
        qsos = [
            self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS'),
            self._qso(datetime(2026, 7, 6, 16, 1), 'HA7NS'),
        ]
        log = [
            _qso_ev(datetime(2026, 7, 6, 16, 1, 10), 'HA7NS'),
            _qso_ev(datetime(2026, 7, 6, 16, 1, 50), 'HA7NS'),
        ]
        times = match_qso_times(qsos, log)
        assert times == [datetime(2026, 7, 6, 16, 1, 10), datetime(2026, 7, 6, 16, 1, 50)]


class TestQsoWindowsPreciseAnchor:
    def test_precise_time_used_as_snap_anchor_instead_of_edi_minute(self):
        # Burst starts at 26.0s; the EDI-minute-derived approx time would
        # map to audio_t=0 (wall-clock rounds down to the segment start),
        # landing _snap_to_cluster on the wrong (or no) earlier cluster. An
        # exact submit time mapping into the real burst fixes the anchor.
        segs = [
            Segment('a', datetime(2026, 7, 6, 16, 1, 0), 26.0, 0.0),      # gap
            Segment('b', datetime(2026, 7, 6, 16, 1, 26), 5.0, 26.0,
                    events=[CharEvent(0.5, 'H')]),                        # the real over
        ]
        q = Qso(datetime(2026, 7, 6, 16, 1), 'HA7NS', '59', '1', '59', '2',
                'JN97MM', 10, False)
        precise = datetime(2026, 7, 6, 16, 1, 28)  # submitted 2s into the over
        [(start, _end)] = qso_windows([q], segs, offset_h=0, total=31.0,
                                      qso_times=[precise])
        assert start == 26.0

    def test_falls_back_to_edi_time_when_unmatched(self):
        segs = [Segment('a', datetime(2026, 7, 6, 16, 1, 0), 10.0, 0.0,
                        events=[CharEvent(0.5, 'H')])]
        q = Qso(datetime(2026, 7, 6, 16, 1), 'HA7NS', '59', '1', '59', '2',
                'JN97MM', 10, False)
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
            Segment('a', datetime(2026, 7, 6, 16, 1, 0), 5.0, 0.0,
                    events=[CharEvent(0.5, 'H')]),
            Segment('b', datetime(2026, 7, 6, 16, 1, 5), 50.0, 5.0),   # real gap
            Segment('c', datetime(2026, 7, 6, 16, 1, 55), 5.0, 55.0,
                    events=[CharEvent(0.5, 'H')]),
        ]
        q1 = Qso(datetime(2026, 7, 6, 16, 1), 'HA7NS', '59', '1', '59', '2',
                'JN97MM', 10, False)
        q2 = Qso(datetime(2026, 7, 6, 16, 1), 'HA3KHB', '59', '2', '59', '2',
                'JN97MM', 10, False)
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
            Segment('a', datetime(2026, 7, 6, 16, 1, 0), 26.0, 0.0),      # gap
            Segment('b', datetime(2026, 7, 6, 16, 1, 26), 5.0, 26.0,
                    events=[CharEvent(0.5, 'H')]),                        # the whole shared burst
        ]
        q1 = Qso(datetime(2026, 7, 6, 16, 1), 'HA3KHB', '59', '1', '59', '2',
                'JN97MM', 10, False)
        q2 = Qso(datetime(2026, 7, 6, 16, 1), 'HA3KHB', '59', '2', '59', '2',
                'JN97MM', 10, False)
        times = [datetime(2026, 7, 6, 16, 1, 28), datetime(2026, 7, 6, 16, 1, 29)]
        windows = qso_windows([q1, q2], segs, offset_h=0, total=31.0, qso_times=times)
        assert windows == [(26.0, 28.0), (28.0, 29.0)]
        # explicitly: no overlap, no gap, and QSO 2 clears well before `total`
        (s1, e1), (s2, e2) = windows
        assert e1 == s2
        assert e2 < 31.0


class TestRunningScore:
    def _qso(self, call, pts, dup=False):
        return Qso(datetime(2026, 7, 6, 16, 1), call, '59', '1', '59', '2',
                   'JN97MM', pts, dup)

    def test_accumulates_count_and_points(self):
        qsos = [self._qso('A', 100), self._qso('B', 50), self._qso('C', 25)]
        assert running_score(qsos) == [(1, 100), (2, 150), (3, 175)]

    def test_dup_counts_but_does_not_score(self):
        qsos = [self._qso('A', 100), self._qso('A', 100, dup=True)]
        assert running_score(qsos) == [(1, 100), (2, 100)]

    def test_empty(self):
        assert running_score([]) == []

    def test_build_ass_header_shows_running_score(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;2]\n"
            "260706;1601;HG7F;2;599;001;599;010;;JN97KR;100;;;;\n"
            "260706;1605;HA3KHB;2;599;002;599;011;;JN86SR;50;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [
            Segment('a', datetime(2026, 7, 6, 16, 1, 0), 240.0, 0.0,
                    events=[CharEvent(1.0, 'H')]),
            Segment('b', datetime(2026, 7, 6, 16, 5, 0), 60.0, 240.0,
                    events=[CharEvent(1.0, 'H')]),
        ]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 0, 1920, 1080)
        header_lines = [line for line in ass.splitlines()
                        if line.startswith('Dialogue:') and ',Header,' in line]
        texts = [line.rsplit(',', 1)[-1] for line in header_lines]
        assert any('1Q 100pts' in t for t in texts)
        assert any('2Q 150pts' in t for t in texts)

    def test_score_appears_at_the_qsos_finish_not_its_panel_start(self):
        # Regression test for a real reported bug: points used to appear as
        # soon as a QSO's panel started (i.e. as soon as the over began),
        # crediting a contact before it was actually complete. With an
        # exact qso_times finish available, the score must only update
        # once that QSO is actually done.
        segs = [Segment('a', datetime(2026, 7, 6, 16, 1, 0), 10.0, 0.0,
                        events=[CharEvent(0.5, 'H')])]
        q1 = Qso(datetime(2026, 7, 6, 16, 1), 'HG7F', '599', '1', '599', '10',
                'JN97KR', 100, False)
        qso_times = [datetime(2026, 7, 6, 16, 1, 5)]   # finishes 5s into its own panel
        ass = build_ass(segs, [q1], 'HA5LA', 'JN97MM', 'TEST', 0, 1920, 1080,
                        qso_times=qso_times)
        header_lines = [line for line in ass.splitlines()
                        if line.startswith('Dialogue:') and ',Header,' in line]
        assert len(header_lines) == 2
        before, after = header_lines
        assert before.startswith('Dialogue: 0,0:00:00.00,0:00:05.00,')
        assert '100pts' not in before
        assert after.startswith('Dialogue: 0,0:00:05.00,')
        assert '1Q 100pts' in after

    def test_build_ass_header_has_no_score_before_first_qso_or_with_none(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 6, 16, 1, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 0, 1920, 1080)
        header_lines = [line for line in ass.splitlines()
                        if line.startswith('Dialogue:') and ',Header,' in line]
        assert len(header_lines) == 1
        assert 'pts' not in header_lines[0]
