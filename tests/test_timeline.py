"""Tests for the round's timeline: segments, EDI, and wall clock to audio time."""

from datetime import datetime

import contest_video as cv
from urhpk import timeline as tl
from urhpk.cw_decode import (
    CharEvent,
)
from urhpk.timeline import (
    GAP_KEEP_S,
    Segment,
    _eff,
    audio_time_for,
    derive_utc_offset,
    merge_edi,
    parse_edi,
    remap_audio_t,
    trim_to_duration,
)


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
        my_callsign, mywwl, qsos = parse_edi(str(edi))
        assert (my_callsign, mywwl) == ("HA5LA", "JN97MM")
        assert len(qsos) == 2
        assert qsos[0].callsign == "HG7F" and qsos[0].pts == 26
        assert qsos[0].dt == datetime(2026, 7, 4, 9, 8)
        assert qsos[1].dup is True and qsos[1].pts == 0

    def test_merge_edi_combines_and_sorts_multiple_bands(self, tmp_path):
        # A round worked on two bands writes two EDI files -- one physical
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
        my_callsign, mywwl, qsos = merge_edi([str(band_2m), str(band_70cm)])
        assert (my_callsign, mywwl) == ("HA5LA", "JN97TF")
        assert [q.callsign for q in qsos] == [
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
            tl.Qso(
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
            tl.Qso(
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


class TestStreamPrecedesAudio:
    """A cast/scope stream that began *before* the first WAV segment must be
    entered partway in, not clamped to video t=0 -- the clamp showed up as
    the cast PiP's clock lagging the round by exactly cast-to-WAV gap
    (25 s in the dry-run that caught it). run-recorded-round.sh
    guarantees this ordering: asciinema starts before the radio recorder."""

    def _segs(self):
        return [tl.Segment("a.wav", datetime(2026, 8, 6, 19, 16, 0), 330.0, 0.0)]

    def test_stream_start_is_negative_before_first_segment(self):
        assert tl.stream_start(datetime(2026, 8, 6, 19, 15, 35), self._segs()) == -25.0

    def test_stream_start_matches_audio_time_for_inside_the_recording(self):
        wall = datetime(2026, 8, 6, 19, 17, 0)
        segs = self._segs()
        assert tl.stream_start(wall, segs) == tl.audio_time_for(wall, segs)

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
