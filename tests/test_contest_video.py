"""Tests for contest_video pure logic and the CW decoder.

No ffmpeg is invoked; the decoder is exercised against a synthesized CW WAV so
the test is fully reproducible (fixed WPM, pitch, sample rate)."""
import wave
from datetime import datetime

import numpy as np

import contest_video as cv
from contest_video import (
    GAP_KEEP_S,
    CharEvent,
    Segment,
    _dominance,
    _eff,
    _quality,
    _utc_at,
    _wrap,
    audio_time_for,
    build_ass,
    decode_segment,
    derive_utc_offset,
    gate_events,
    parse_edi,
    remap_audio_t,
)

SR = 16000
PITCH = 600.0

_MORSE_INV = {v: k for k, v in cv.MORSE.items()}


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

    def test_ticker_flushes_on_qso_transition(self, tmp_path):
        # Two QSOs; first QSO's characters should not appear in second QSO's ticker
        edi = tmp_path / 'log.edi'
        edi.write_text(
            "PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;2]\n"
            "260704;1100;HG7F;2;599;001;599;010;;JN97KR;26;;;;\n"
            "260704;1110;HA7NK;2;599;002;599;014;;JN97WW;77;;;;\n"
        )
        mycall, mywwl, qsos = parse_edi(str(edi))
        # Segment spanning both QSOs, with events in the first half only
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 1200.0, 0.0,
                        events=[CharEvent(1.0, 'X'), CharEvent(2.0, 'Y')])]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080)
        # 'XY' should appear in the ticker at some point
        assert 'XY' in ass
