"""Tests for contest_video pure logic and the CW decoder.

No ffmpeg is invoked; the decoder is exercised against a synthesized CW WAV so
the test is fully reproducible (fixed WPM, pitch, sample rate)."""
import wave
from datetime import datetime

import numpy as np

import contest_video as cv
from contest_video import (
    CAPTION_DUR_S,
    GAP_KEEP_S,
    MAX_OVER_S,
    CharEvent,
    Qso,
    Segment,
    SegState,
    TelemetrySample,
    _dominance,
    _eff,
    _quality,
    _srt_time,
    _utc_at,
    _wrap,
    _yt_time,
    align_telemetry_to_segments,
    audio_time_for,
    build_ass,
    build_chapters,
    build_srt,
    cluster_starts,
    decode_segment,
    derive_utc_offset,
    gate_events,
    load_telemetry,
    merge_edi,
    parse_edi,
    parse_webcam_wall,
    qso_windows,
    remap_audio_t,
    sync_webcam_start,
    trim_to_duration,
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


class TestTelemetryAlignment:
    def test_load_telemetry_parses_lines_and_skips_bad_ones(self, tmp_path):
        f = tmp_path / 'telem.jsonl'
        f.write_text(
            '{"t": "2026-07-04T11:00:02Z", "ptt": true, "freq_hz": 144174000, '
            '"mode": "CW", "az": 135.0}\n'
            'not json\n'
            '{"t": "2026-07-04T11:00:05Z", "ptt": false}\n'
        )
        samples = load_telemetry(str(f))
        assert len(samples) == 2
        assert samples[0] == TelemetrySample(
            datetime(2026, 7, 4, 11, 0, 2), True, 144174000, 'CW', 135.0)
        assert samples[1].ptt is False
        assert samples[1].freq_hz is None

    def test_align_majority_and_median_inside_segment(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 2), True, 144174000, 'CW', 135.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 5), True, 144174000, 'CW', 136.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 8), False, 144174000, 'CW', 137.0),
        ]
        [st] = align_telemetry_to_segments(segs, telemetry, offset_h=2)
        assert st.ptt is True          # 2 TX vs 1 RX
        assert st.freq_hz == 144174000  # unanimous
        assert st.mode == 'CW'
        assert st.az == 136.0          # median of 135/136/137

    def test_align_falls_back_to_nearest_sample_outside_short_segment(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 10), 2.0, 0.0)]
        telemetry = [
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 9), False, 145600000, 'FM', 90.0),
            TelemetrySample(datetime(2026, 7, 4, 11, 0, 20), True, 144174000, 'CW', 45.0),
        ]
        [st] = align_telemetry_to_segments(segs, telemetry, offset_h=2)
        assert st.ptt is False
        assert st.freq_hz == 145600000

    def test_align_returns_all_none_without_telemetry(self):
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        [st] = align_telemetry_to_segments(segs, [], offset_h=2)
        assert st == SegState()

    def test_build_ass_includes_state_badge_and_rig_info(self, tmp_path):
        edi = tmp_path / 'log.edi'
        edi.write_text("PCall=HA5LA\nPWWLo=JN97MM\n[QSORecords;0]\n")
        mycall, mywwl, qsos = parse_edi(str(edi))
        segs = [Segment('a', datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0)]
        ass = build_ass(segs, qsos, mycall, mywwl, 'TEST', 2, 1920, 1080,
                        seg_states=[SegState(True, 144174000, 'CW', 135.0)])
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
                        seg_states=[SegState()])
        assert ',State,' not in ass
