"""Tests for placing each QSO on the video timeline."""

from datetime import datetime

from cw_decode import (
    MAX_OVER_S,
    CharEvent,
)
from qso_windows import cluster_starts, qso_windows
from timeline import (
    Qso,
    Segment,
    parse_edi,
)


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
        my_callsign, mywwl, qsos = parse_edi(str(edi))
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
        my_callsign, mywwl, qsos = parse_edi(str(edi))
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
        my_callsign, mywwl, qsos = parse_edi(str(edi))
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
