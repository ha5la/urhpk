"""Tests for what the rig and rotator were doing: telemetry, input log, QSO times."""

from datetime import datetime

from urhpk.rig_state import (
    InputLogEvent,
    TelemetrySample,
    build_state_events,
    load_input_log,
    load_telemetry,
    match_qso_times,
)
from urhpk.timeline import (
    Qso,
    Segment,
)


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

    def test_a_clock_offset_record_disturbs_nothing_that_reads_telemetry(
        self, tmp_path
    ):
        # puskas_logger writes {"t", "clock_offset_s"} lines into the same
        # file. They mention none of the fields the render reads, and a
        # missing field means "nothing changed" -- but a *rig-offline* record
        # is an explicit {"freq_hz": null, "mode": null}, which parses to the
        # same all-None sample. Nothing downstream may confuse the two.
        rig_line = '{"t": "2026-07-04T11:00:02Z", "freq_hz": 144174000, "mode": "CW"}\n'
        clock_line = '{"t": "2026-07-04T11:00:04Z", "clock_offset_s": -0.19}\n'

        without = tmp_path / "without.jsonl"
        without.write_text(rig_line)
        with_clock = tmp_path / "with.jsonl"
        with_clock.write_text(rig_line + clock_line)

        segs = [
            self._wav_seg(
                datetime(2026, 7, 4, 11, 0, 0), 10.0, 0.0, 144174000, "CW", True
            )
        ]
        assert build_state_events(
            segs, load_telemetry(str(with_clock)), offset_h=0
        ) == build_state_events(segs, load_telemetry(str(without)), offset_h=0)

        # and it claims neither a rotator nor a meter reading, which the HUD
        # series select on
        (clock_sample,) = [
            t for t in load_telemetry(str(with_clock)) if t.t.second == 4
        ]
        assert clock_sample.az is None and not clock_sample.az_offline
        assert clock_sample.vd is None and not clock_sample.meters_offline

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


def _qso_ev(t, callsign, dup=False):
    return InputLogEvent(t, "qso", callsign=callsign, dup=dup)


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
                datetime(2026, 7, 4, 11, 0, 5), "qso", callsign="HA7NS", dup=False
            ),
        ]

    def test_load_input_log_defaults_missing_event_field_to_text(self, tmp_path):
        # Written before the "event" field existed, or hand-crafted -- treat
        # as a keystroke rather than dropping it.
        f = tmp_path / "input.jsonl"
        f.write_text('{"t": "2026-07-04T11:00:02.000000Z", "text": "H"}\n')
        log = load_input_log(str(f))
        assert log == [InputLogEvent(datetime(2026, 7, 4, 11, 0, 2), "text", text="H")]


class TestMatchQsoTimes:
    def _qso(self, dt, callsign):
        return Qso(dt, callsign, "59", "1", "59", "2", "JN97MM", 10, False)

    def test_matches_by_call_for_a_single_occurrence(self):
        qsos = [self._qso(datetime(2026, 7, 6, 16, 1), "HA7NS")]
        log = [_qso_ev(datetime(2026, 7, 6, 16, 1, 42, 123456), "HA7NS")]
        [t] = match_qso_times(qsos, log)
        assert t == datetime(2026, 7, 6, 16, 1, 42, 123456)

    def test_matches_when_the_log_and_the_edi_disagree_on_the_minute(self):
        # Normally they agree exactly, both deriving from one captured `now`.
        # Matching must not *depend* on that: a hand-written or edited log can
        # put the QSO in a different minute than the EDI recorded, and getting
        # nothing back would be a silent, total failure rather than a visible
        # one.
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
