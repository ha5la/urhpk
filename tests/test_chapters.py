"""Tests for the YouTube chapters and the SRT captions."""

from datetime import datetime

from chapters import CAPTION_DUR_S, _srt_time, _yt_time, build_chapters, build_srt
from qso_windows import qso_windows
from timeline import (
    Qso,
    Segment,
    parse_edi,
)


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
