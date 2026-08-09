"""Tests for puskas_logger pure functions — no rig, no network, no prompts."""

import io
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import loc_cache
import puskas_logger as pl
import recorders
import rig_server
import rotator
from geo import haversine_km, initial_bearing, is_locator, maidenhead_to_latlon
from logbook import (
    QSO,
    LogBook,
    _is_dup_in_log,
    band_summary,
    load_from_edi,
    tname_for,
    write_edi,
)
from puskas_logger import (
    _bearing_arrow,
    _edi_qso_count,
    _format_combos,
    _is_contest_time,
    _predict_nr,
    _print_recent,
    _rig,
    _rig_lock,
    parse_input,
)
from recorders import (
    _webcam_capture_cmd,
    _webcam_precise_name,
    _webcam_precise_start,
    input_log_open,
    on_buffer_changed,
    telemetry_rig_record,
    telemetry_rot_record,
    webcam_toggle,
)

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _dt(h: int = 16, m: int = 0) -> datetime:
    return datetime(2026, 5, 4, h, m, tzinfo=timezone.utc)


def _qso(
    callsign="HA7NS",
    band="2M",
    mode="SSB",
    nr_s=1,
    nr_r=1,
    rst_s="59",
    rst_r="59",
    loc="JN97WM",
    dist_km=38,
    h=16,
    m=0,
    dt=None,
) -> QSO:
    return QSO(
        dt=dt or _dt(h, m),
        band=band,
        mode=mode,
        callsign=callsign,
        rst_s=rst_s,
        nr_s=nr_s,
        rst_r=rst_r,
        nr_r=nr_r,
        loc=loc,
        dist_km=dist_km,
    )


# ──────────────────────────────────────────────────────────────
# parse_input
# ──────────────────────────────────────────────────────────────


class TestParseInput:
    def test_three_tokens_no_locator_returns_error(self):
        r = parse_input("HA7NS 59 015")
        assert "Usage" in r

    def test_with_locator(self):
        r = parse_input("HA7NS 59 015 JN97WM")
        assert r == {"callsign": "HA7NS", "rst_r": "59", "nr_r": 15, "loc": "JN97WM"}

    def test_cw_rst(self):
        r = parse_input("HA7NS 599 014 JN97WM")
        assert r == {"callsign": "HA7NS", "rst_r": "599", "nr_r": 14, "loc": "JN97WM"}

    def test_lowercase_input_normalised(self):
        r = parse_input("ha7ns 59 001 jn97wm")
        assert r["callsign"] == "HA7NS"
        assert r["loc"] == "JN97WM"

    def test_four_char_locator_accepted(self):
        r = parse_input("HA7NS 59 001 JN97")
        assert r["loc"] == "JN97"

    def test_eight_char_locator_not_accepted(self):
        # is_locator is anchored — an 8-char string doesn't match, so loc stays empty → error
        r = parse_input("HA7NS 59 001 JN97WMXX")
        assert "Usage" in r

    def test_portable_callsign(self):
        r = parse_input("HA5LA/P 59 007 JN97TF")
        assert r["callsign"] == "HA5LA/P"

    def test_empty_line_returns_empty_string(self):
        assert parse_input("") == ""
        assert parse_input("   ") == ""

    def test_one_token_returns_error(self):
        r = parse_input("HA7NS")
        assert "Usage" in r

    def test_two_tokens_returns_error(self):
        r = parse_input("HA7NS 59")
        assert "Usage" in r

    def test_invalid_callsign_returns_error(self):
        r = parse_input("!BAD 59 001")
        assert "Invalid callsign" in r

    def test_pure_digit_callsign_returns_error(self):
        r = parse_input("123 59 001")
        assert "Invalid callsign" in r

    def test_non_numeric_nr_returns_error(self):
        r = parse_input("HA7NS 59 ABC")
        assert "serial" in r.lower()

    def test_zero_nr_returns_error(self):
        r = parse_input("HA7NS 59 000")
        assert isinstance(r, str) and r

    def test_nr_too_large_returns_error(self):
        r = parse_input("HA7NS 59 99999")
        assert isinstance(r, str) and r

    def test_rst_is_verbatim(self):
        r = parse_input("HA7NS 57 003 JN97WM")
        assert r["rst_r"] == "57"

    def test_extra_tokens_before_locator_ignored(self):
        # locator is first Maidenhead-matching token in tokens[3:]
        r = parse_input("HA7NS 59 001 NOISE JN97WM")
        assert r["loc"] == "JN97WM"


# ──────────────────────────────────────────────────────────────
# LogBook
# ──────────────────────────────────────────────────────────────


class TestLogBook:
    def setup_method(self):
        self.lb = LogBook("HA5LA", "JN97TF", {"HA7NS": ["JN97WM"]})

    def test_next_nr_starts_at_one(self):
        assert self.lb.next_nr("2M") == 1

    def test_next_nr_increments_per_band(self):
        self.lb.add(_qso(band="2M", nr_s=1))
        assert self.lb.next_nr("2M") == 2
        assert self.lb.next_nr("70CM") == 1

    def test_next_nr_no_band_returns_total_plus_one(self):
        self.lb.add(_qso(band="2M", nr_s=1))
        self.lb.add(_qso(band="70CM", nr_s=1))
        assert self.lb.next_nr("") == 3

    def test_add_returns_false_for_new_qso(self):
        assert self.lb.add(_qso()) is False

    def test_add_returns_true_for_dup(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB"))
        assert self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB")) is True

    def test_dup_check_uses_all_three_keys(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB"))
        assert self.lb.add(_qso(callsign="HA7NS", band="70CM", mode="SSB")) is False
        assert self.lb.add(_qso(callsign="HA7NS", band="2M", mode="CW")) is False
        assert self.lb.add(_qso(callsign="HA7NS", band="2M", mode="FM")) is False

    def test_nine_valid_combos_per_station(self):
        combos = [(b, m) for b in ("2M", "70CM", "23CM") for m in ("SSB", "CW", "FM")]
        assert len(combos) == 9
        for b, m in combos:
            assert self.lb.add(_qso(callsign="HA7NS", band=b, mode=m)) is False

    def test_worked_combos_empty_for_never_worked_call(self):
        # nothing worked yet -> nothing to warn about
        assert self.lb.worked_combos("HA7NS") == {}

    def test_worked_combos_lists_worked_modes_by_band(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB"))
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="CW"))
        self.lb.add(_qso(callsign="HA7NS", band="70CM", mode="CW"))
        assert self.lb.worked_combos("HA7NS") == {
            "2M": ["SSB", "CW"],
            "70CM": ["CW"],
        }

    def test_worked_combos_omits_bands_with_nothing_worked(self):
        self.lb.add(_qso(callsign="HA7NS", band="70CM", mode="SSB"))
        combos = self.lb.worked_combos("HA7NS")
        assert "2M" not in combos and "23CM" not in combos
        assert combos["70CM"] == ["SSB"]

    def test_worked_combos_lists_all_nine_once_all_worked(self):
        for b in ("2M", "70CM", "23CM"):
            for m in ("SSB", "CW", "FM"):
                self.lb.add(_qso(callsign="HA7NS", band=b, mode=m))
        assert self.lb.worked_combos("HA7NS") == {
            "2M": ["SSB", "CW", "FM"],
            "70CM": ["SSB", "CW", "FM"],
            "23CM": ["SSB", "CW", "FM"],
        }

    def test_worked_combos_is_per_callsign(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB"))
        assert self.lb.worked_combos("HA3KHB") == {}

    def test_undo_removes_last_qso(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1))
        self.lb.add(_qso(callsign="HA3KHB", band="2M", mode="SSB", nr_s=2))
        q = self.lb.undo()
        assert q.callsign == "HA3KHB"
        assert len(self.lb.qsos) == 1

    def test_undo_rebuilds_worked_set(self):
        self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB"))
        self.lb.undo()
        assert self.lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB")) is False

    def test_undo_on_empty_returns_none(self):
        assert self.lb.undo() is None

    def test_bands_returns_order_of_first_appearance(self):
        self.lb.add(_qso(band="70CM"))
        self.lb.add(_qso(band="2M"))
        assert self.lb.bands() == ["70CM", "2M"]

    def test_dist_uses_haversine(self):
        # JN97TF → JN97WM should be around 20-50 km
        d = self.lb.dist("JN97WM")
        assert 10 < d < 60

    def test_dist_zero_without_locators(self):
        lb = LogBook("HA5LA", "", {})
        assert lb.dist("JN97WM") == 0
        lb2 = LogBook("HA5LA", "JN97TF", {})
        assert lb2.dist("") == 0

    def test_bearing_northwest_to_io83(self):
        # JN97TF → IO83RO is northwest (~302°)
        b = self.lb.bearing("IO83RO")
        assert 290 < b < 320

    def test_bearing_zero_without_locators(self):
        lb = LogBook("HA5LA", "", {})
        assert lb.bearing("JN97WM") == 0
        lb2 = LogBook("HA5LA", "JN97TF", {})
        assert lb2.bearing("") == 0


# ──────────────────────────────────────────────────────────────
# _is_dup_in_log
# ──────────────────────────────────────────────────────────────


class TestIsDupInLog:
    def test_first_occurrence_is_not_dup(self):
        q = _qso(callsign="HA7NS", band="2M", mode="SSB")
        assert _is_dup_in_log([q], q) is False

    def test_second_occurrence_is_dup(self):
        q1 = _qso(callsign="HA7NS", band="2M", mode="SSB", h=16)
        q2 = _qso(callsign="HA7NS", band="2M", mode="SSB", h=17)
        assert _is_dup_in_log([q1, q2], q2) is True

    def test_different_band_not_dup(self):
        q1 = _qso(callsign="HA7NS", band="2M", mode="SSB")
        q2 = _qso(callsign="HA7NS", band="70CM", mode="SSB")
        assert _is_dup_in_log([q1, q2], q2) is False


# ──────────────────────────────────────────────────────────────
# tname_for
# ──────────────────────────────────────────────────────────────


class TestTnameFor:
    def test_may_2026(self):
        assert tname_for(datetime(2026, 5, 4, tzinfo=timezone.utc)) == "PUSKAS2026MAJUS"

    def test_january(self):
        assert (
            tname_for(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "PUSKAS2026JANUAR"
        )

    def test_december(self):
        assert (
            tname_for(datetime(2025, 12, 8, tzinfo=timezone.utc))
            == "PUSKAS2025DECEMBER"
        )


# ──────────────────────────────────────────────────────────────
# write_edi
# ──────────────────────────────────────────────────────────────


class TestWriteEdi:
    def setup_method(self, tmp_path_factory):
        # Use pytest's tmp_path fixture via a workaround; tests use self.tmp_path
        pass

    def test_writes_file(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(
            _qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, nr_r=1, dist_km=38)
        )
        p = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        assert p is not None and p.exists()

    def test_filename_convention(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", nr_s=1))
        p = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        assert p.name == "260504-HA5LA-2M.edi"

    def test_header_fields(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", nr_s=1, nr_r=1, dist_km=38))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "TName=PUSKAS2026MAJUS" in txt
        assert "PCall=HA5LA" in txt
        assert "PWWLo=JN97TF" in txt
        assert "PBand=145 MHz" in txt

    def test_qso_record_format(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(
            _qso(
                callsign="HA7NS",
                band="2M",
                mode="SSB",
                nr_s=1,
                nr_r=1,
                rst_s="59",
                rst_r="59",
                loc="JN97WM",
                dist_km=38,
            )
        )
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "260504;1600;HA7NS;1;59;001;59;001;;JN97WM;38" in txt

    def test_cw_mode_code(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", mode="CW", rst_s="599", rst_r="599", nr_s=1))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert ";2;" in txt  # mode code 2 = CW

    def test_fm_mode_code(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", mode="FM", nr_s=1))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert ";6;" in txt  # mode code 6 = FM

    def test_dup_flagged_with_d(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, h=16))
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=2, h=17))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        lines = [line for line in txt.splitlines() if "HA7NS" in line]
        assert len(lines) == 2
        assert lines[0].endswith(";")  # not a dup
        assert lines[1].endswith("D;")  # dup

    def test_dup_excluded_from_score(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, dist_km=38, h=16))
        lb.add(
            _qso(callsign="HA3KHB", band="2M", mode="SSB", nr_s=2, dist_km=168, h=17)
        )
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=3, dist_km=38, h=18))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "CQSOP=206" in txt  # 38 + 168; dup not counted

    def test_qso_count_header(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(nr_s=1, h=16))
        lb.add(_qso(nr_s=2, h=17))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "CQSOs=2;1" in txt
        assert "[QSORecords;2]" in txt

    def test_70cm_band_frequency(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="70CM", nr_s=1))
        txt = write_edi(lb, "70CM", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "PBand=435 MHz" in txt

    def test_returns_none_for_empty_band(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M"))
        assert write_edi(lb, "70CM", "PUSKAS2026MAJUS", tmp_path) is None


# ──────────────────────────────────────────────────────────────
# load_from_edi (roundtrip)
# ──────────────────────────────────────────────────────────────


class TestLoadFromEdi:
    def _make_logbook(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(
            _qso(
                callsign="HA7NS",
                band="2M",
                mode="SSB",
                nr_s=1,
                nr_r=1,
                dist_km=38,
                h=16,
                m=1,
            )
        )
        lb.add(
            _qso(
                callsign="HA3KHB",
                band="2M",
                mode="CW",
                nr_s=2,
                nr_r=14,
                dist_km=168,
                h=16,
                m=59,
                rst_s="599",
                rst_r="599",
            )
        )
        lb.add(
            _qso(
                callsign="HA7NS",
                band="2M",
                mode="SSB",
                nr_s=3,
                nr_r=2,
                dist_km=38,
                h=17,
                m=5,
            )
        )
        return lb

    def test_roundtrip_preserves_qso_count(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        paths = list(tmp_path.glob("*.[Ee][Dd][Ii]"))
        result = load_from_edi(paths, {})
        assert result is not None
        lb2, tname = result
        assert len(lb2.qsos) == 3

    def test_roundtrip_preserves_callsign_and_locator(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert lb2.my_callsign == "HA5LA"
        assert lb2.my_loc == "JN97TF"

    def test_roundtrip_preserves_tname(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        _, tname = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert tname == "PUSKAS2026MAJUS"

    def test_roundtrip_rebuilds_dup_state(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        # HA7NS SSB 2M was worked first → second entry is a dup
        assert ("HA7NS", "2M", "SSB") in lb2.worked
        # adding again should be detected as dup
        assert lb2.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=4)) is True

    def test_roundtrip_next_nr_continues(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert lb2.next_nr("2M") == 4

    def test_multiband_roundtrip(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, h=16))
        lb.add(_qso(callsign="HA7NS", band="70CM", mode="FM", nr_s=1, h=17))
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        write_edi(lb, "70CM", "PUSKAS2026MAJUS", tmp_path)
        paths = sorted(tmp_path.glob("*.[Ee][Dd][Ii]"))
        lb2, _ = load_from_edi(paths, {})
        assert len(lb2.qsos) == 2
        assert lb2.next_nr("2M") == 2
        assert lb2.next_nr("70CM") == 2

    def test_returns_none_for_empty_list(self):
        assert load_from_edi([], {}) is None

    def test_qso_without_locator_is_rejected(self, tmp_path):
        # Manually craft an EDI file with one valid and one locator-free record.
        edi = (
            "PCall=HA5LA\nPWWLo=JN97TF\nTName=TEST\nPBand=145 MHz\n"
            "[QSORecords;2]\n"
            "260601;1800;HA7NS;1;59;001;59;001;;JN97WM;38;;;\n"
            "260601;1801;HA3KHB;1;59;002;59;002;;   ;0;;;\n"  # empty locator
        )
        p = tmp_path / "test.edi"
        p.write_text(edi)
        lb2, _ = load_from_edi([p], {})
        assert len(lb2.qsos) == 1
        assert lb2.qsos[0].callsign == "HA7NS"

    def test_uppercase_and_lowercase_edi_not_doubled(self, tmp_path):
        """Coexisting foo.EDI and foo.edi must not double the QSOs on recovery.

        The scan is case-insensitive because the format's own convention is
        uppercase, so both spellings genuinely turn up in contest directories.
        """
        lb = self._make_logbook()
        p = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        stale = p.with_suffix(".EDI")
        stale.write_text(p.read_text())
        paths = sorted(tmp_path.glob("*.[Ee][Dd][Ii]"))
        assert len(paths) == 2  # both files visible on Linux
        lb2, _ = load_from_edi(paths, {})
        assert len(lb2.qsos) == len(lb.qsos)  # no doubling

    def test_loc_cache_passed_through(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        cache = {"HA7NS": ["JN97WM"]}
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), cache)
        assert lb2.loc_cache == cache


# ──────────────────────────────────────────────────────────────
# QSO editing (inline edit logic from run())
# ──────────────────────────────────────────────────────────────


class TestQsoEdit:
    def _lb_with_qsos(self):
        lb = LogBook("HA5LA", "JN97TF", {"HA7NS": ["JN97WM"], "HA3KHB": ["JN86SR"]})
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, nr_r=1, h=16, m=1))
        lb.add(
            _qso(callsign="HA3KHB", band="2M", mode="SSB", nr_s=2, nr_r=14, h=16, m=59)
        )
        lb.add(
            _qso(callsign="HA8RM", band="2M", mode="SSB", nr_s=3, nr_r=12, h=17, m=4)
        )
        return lb

    def _apply_edit(self, lb, edit_idx, parsed):
        real_idx = len(lb.qsos) - 1 - edit_idx
        old = lb.qsos[real_idx]
        loc = parsed["loc"]  # mandatory
        lb.qsos[real_idx] = QSO(
            dt=old.dt,
            band=old.band,
            mode=old.mode,
            callsign=parsed["callsign"],
            rst_s=old.rst_s,
            nr_s=old.nr_s,
            rst_r=parsed["rst_r"],
            nr_r=parsed["nr_r"],
            loc=loc,
            dist_km=lb.dist(loc),
        )
        lb.worked = {(q.callsign, q.band, q.mode) for q in lb.qsos}

    def test_edit_last_callsign_typo(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA8RM 59 012 JN96UW")
        # edit_idx=0 → last QSO (HA8RM was logged with wrong loc, fix it)
        self._apply_edit(lb, 0, parsed)
        assert lb.qsos[2].callsign == "HA8RM"
        assert lb.qsos[2].loc == "JN96UW"

    def test_edit_preserves_dt_band_mode_nr_s_rst_s(self):
        lb = self._lb_with_qsos()
        original = lb.qsos[0]
        parsed = parse_input("HA7NS 59 002 JN97WM")
        self._apply_edit(lb, 2, parsed)  # edit_idx=2 → first QSO
        edited = lb.qsos[0]
        assert edited.dt == original.dt
        assert edited.band == original.band
        assert edited.mode == original.mode
        assert edited.nr_s == original.nr_s
        assert edited.rst_s == original.rst_s

    def test_edit_middle_qso(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA3KHB 59 015 JN86SR")
        self._apply_edit(lb, 1, parsed)  # edit_idx=1 → middle QSO
        assert lb.qsos[1].nr_r == 15
        assert lb.qsos[0].callsign == "HA7NS"  # others unchanged
        assert lb.qsos[2].callsign == "HA8RM"

    def test_edit_rebuilds_worked_set(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA5OO 59 012 JN96UW")
        self._apply_edit(lb, 0, parsed)  # replace HA8RM with HA5OO
        assert ("HA5OO", "2M", "SSB") in lb.worked
        assert ("HA8RM", "2M", "SSB") not in lb.worked

    def test_edit_fixes_callsign_dup_detection(self):
        lb = self._lb_with_qsos()
        # HA7NS is in worked; edit first QSO to change its callsign
        parsed = parse_input("HA5OO 59 001 JN96UW")
        self._apply_edit(lb, 2, parsed)  # edit_idx=2 → first QSO
        # HA7NS should no longer be in worked (it was the only one)
        assert ("HA7NS", "2M", "SSB") not in lb.worked
        # Adding HA7NS now should not be a dup
        assert lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB")) is False

    def test_missing_locator_returns_error(self):
        r = parse_input("HA7NS 59 001")
        assert "Usage" in r

    def test_edit_roundtrip_via_edi(self, tmp_path):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA8RM 59 012 JN96UW")
        self._apply_edit(lb, 0, parsed)
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert lb2.qsos[2].callsign == "HA8RM"
        assert lb2.qsos[2].loc == "JN96UW"


# ──────────────────────────────────────────────────────────────
# band_summary
# ──────────────────────────────────────────────────────────────


class TestBandSummary:
    def test_no_qsos(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        assert band_summary(lb) == "no QSOs yet"

    def test_single_band(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", dist_km=100, nr_s=1, h=16))
        assert band_summary(lb) == "2M:1q/100pt"

    def test_dups_excluded_from_pts(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(callsign="HA7NS", band="2M", dist_km=100, nr_s=1, h=16))
        lb.add(_qso(callsign="HA7NS", band="2M", dist_km=100, nr_s=2, h=17))  # dup
        assert band_summary(lb) == "2M:2q/100pt"

    def test_three_bands(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", dist_km=100, nr_s=1, h=16))
        lb.add(_qso(band="70CM", dist_km=200, nr_s=1, h=17))
        lb.add(_qso(band="23CM", dist_km=50, nr_s=1, h=18))
        s = band_summary(lb)
        assert "2M:1q/100pt" in s
        assert "70CM:1q/200pt" in s
        assert "23CM:1q/50pt" in s

    def test_fits_in_header_width(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        for i, (band, km) in enumerate(
            [("2M", 9999), ("70CM", 9999), ("23CM", 9999)], 1
        ):
            lb.add(_qso(band=band, dist_km=km, nr_s=i, h=16 + i))
        prefix = " PUSKÁS LOGGER  │  "
        full = prefix + band_summary(lb)
        assert len(full) <= 64


# ──────────────────────────────────────────────────────────────
# _print_recent
# ──────────────────────────────────────────────────────────────


class TestPrintRecent:
    def _lb(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        for i in range(10):
            lb.add(
                _qso(callsign=f"HA{i}AA", nr_s=i + 1, h=14, m=i * 5, dist_km=100 + i)
            )
        return lb

    def _lines(self, lb, **kwargs):
        buf = io.StringIO()
        import sys

        old, sys.stdout = sys.stdout, buf
        try:
            _print_recent(lb, **kwargs)
        finally:
            sys.stdout = old
        return buf.getvalue().splitlines()

    def test_normal_shows_last_n(self):
        lb = self._lb()
        lines = self._lines(lb, n=4)
        data = [line for line in lines if "HA" in line]
        assert len(data) == 4
        assert "HA9AA" in data[-1]  # last QSO at bottom

    def test_focus_row_has_arrow_prefix(self):
        lb = self._lb()
        focus = 5  # 6th QSO (0-indexed)
        lines = self._lines(lb, n=8, focus=focus)
        focused = [line for line in lines if "HA5AA" in line]
        assert len(focused) == 1
        assert focused[0].startswith("> ") or "\033[1m>" in focused[0]

    def test_unfocused_rows_have_space_prefix(self):
        lb = self._lb()
        focus = 5
        lines = self._lines(lb, n=8, focus=focus)
        for line in lines:
            if "HA" in line and "HA5AA" not in line:
                assert not line.lstrip("\033[1m").startswith(">")

    def test_focus_shows_rows_after(self):
        lb = self._lb()
        focus = 3  # middle of log
        lines = self._lines(lb, n=8, focus=focus)
        calls = [line for line in lines if "HA" in line]
        # QSO at index > focus must appear
        assert any("HA4AA" in line or "HA5AA" in line for line in calls)

    def test_focus_near_start_shows_enough_rows(self):
        lb = self._lb()
        lines = self._lines(lb, n=8, focus=1)
        calls = [line for line in lines if "HA" in line]
        assert len(calls) >= 2

    def test_bearing_column_always_shown(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(
            _qso(
                callsign="HA7NS", nr_s=1, h=14, loc="JN97WM", dist_km=lb.dist("JN97WM")
            )
        )
        lines = self._lines(lb, n=4)
        qso_line = next(line for line in lines if "HA7NS" in line)
        assert "°" in qso_line
        assert "km" in qso_line
        # bearing arrow follows "°" — check the char right after the degree sign + space
        deg_pos = qso_line.index("°")
        assert qso_line[deg_pos + 2] in "↑↗→↘↓↙←↖"

    def test_tx_rx_arrows_in_log_line(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(
            _qso(
                callsign="HA7NS", nr_s=1, h=14, loc="JN97WM", dist_km=lb.dist("JN97WM")
            )
        )
        lines = self._lines(lb, n=4)
        qso_line = next(line for line in lines if "HA7NS" in line)
        # ↑ labels the sent RST/NR, ↓ labels the received RST/NR
        assert "↑" in qso_line
        assert "↓" in qso_line
        # ↑ must come before ↓
        assert qso_line.index("↑") < qso_line.index("↓")

    def test_multiband_load_sorted_by_timestamp(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_s=1, h=14, m=0))
        lb.add(_qso(callsign="HA3KHB", band="70CM", mode="FM", nr_s=1, h=14, m=10))
        lb.add(_qso(callsign="HA8RM", band="2M", mode="SSB", nr_s=2, h=14, m=20))
        write_edi(lb, "2M", "T", tmp_path)
        write_edi(lb, "70CM", "T", tmp_path)
        # Load in 70CM-first order to exercise sorting
        paths = sorted(tmp_path.glob("*.[Ee][Dd][Ii]"), reverse=True)
        lb2, _ = load_from_edi(paths, {})
        assert [q.callsign for q in lb2.qsos] == ["HA7NS", "HA3KHB", "HA8RM"]


# ──────────────────────────────────────────────────────────────
# _edi_qso_count
# ──────────────────────────────────────────────────────────────


class TestEdiQsoCount:
    def test_reads_count_from_header(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(nr_s=1, h=16))
        lb.add(_qso(nr_s=2, h=17))
        p = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        assert _edi_qso_count(p) == 2

    def test_missing_file_returns_zero(self, tmp_path):
        assert _edi_qso_count(tmp_path / "nonexistent.edi") == 0


# ──────────────────────────────────────────────────────────────
# _predict_nr
# ──────────────────────────────────────────────────────────────


class TestPredictNr:
    def _lb(self):
        return LogBook("HA5LA", "JN97TF", {})

    def test_no_prior_qso_returns_none(self):
        lb = self._lb()
        assert _predict_nr(lb, "HA7NS", "2M", "CW") is None

    def test_cross_mode_recent_returns_nr_r_plus_one(self):
        lb = self._lb()
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) == 16

    def test_same_mode_not_used(self):
        lb = self._lb()
        lb.add(_qso(callsign="HA7NS", band="2M", mode="CW", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) is None

    def test_different_band_not_used(self):
        lb = self._lb()
        lb.add(_qso(callsign="HA7NS", band="70CM", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) is None

    def test_most_recent_cross_mode_wins(self):
        lb = self._lb()
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_r=10, dt=_dt(16, 0)))
        lb.add(_qso(callsign="HA7NS", band="2M", mode="CW", nr_r=20, dt=_dt(16, 1)))
        # current mode FM, 4 min later: most recent cross-mode is CW/20 → predict 21
        assert _predict_nr(lb, "HA7NS", "2M", "FM", now=_dt(16, 4)) == 21

    def test_old_qso_returns_none(self):
        lb = self._lb()
        lb.add(_qso(callsign="HA7NS", band="2M", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        # 6 minutes later — outside the 5-minute window
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 6)) is None


# ──────────────────────────────────────────────────────────────
# loc_cache.merge_sources
# ──────────────────────────────────────────────────────────────


class TestFromMyLogs:
    """The operator's own past EDI logs are the highest-trust locator source.

    Read through edi.read rather than by splitting the line here — this used
    to hand-parse `[QSORecords` and field 9 itself, a third copy of what
    edi.py owns. Verified equal to the old parser on four real round logs
    before the change; these pin the behaviour that matters.
    """

    def _write(self, tmp_path, name, body):
        d = tmp_path / "my-logs"
        d.mkdir(exist_ok=True)
        (d / name).write_text(
            "[REG1TEST;1]\nPCall=HA5LA\nPWWLo=JN97TF\n[QSORecords;1]\n" + body,
            encoding="utf-8",
        )

    def test_reads_callsign_and_locator_from_a_log(self, tmp_path, monkeypatch):
        self._write(
            tmp_path, "a.edi", "260803;1704;ha7ns;2;599;001;599;001;;jn97wm;37;;;;\n"
        )
        monkeypatch.setattr(loc_cache, "MY_LOGS_DIR", tmp_path / "my-logs")
        assert loc_cache._from_my_logs() == {"HA7NS": "JN97WM"}

    def test_a_later_file_wins(self, tmp_path, monkeypatch):
        self._write(
            tmp_path, "a.edi", "260803;1704;HA7NS;2;599;001;599;001;;JN97WM;37;;;;\n"
        )
        self._write(
            tmp_path, "b.edi", "260901;1704;HA7NS;2;599;001;599;001;;JN88AA;37;;;;\n"
        )
        monkeypatch.setattr(loc_cache, "MY_LOGS_DIR", tmp_path / "my-logs")
        assert loc_cache._from_my_logs() == {"HA7NS": "JN88AA"}

    def test_a_record_without_a_locator_is_skipped(self, tmp_path, monkeypatch):
        self._write(tmp_path, "a.edi", "260803;1704;HA7NS;2;599;001;599;001;;;0;;;;\n")
        monkeypatch.setattr(loc_cache, "MY_LOGS_DIR", tmp_path / "my-logs")
        assert loc_cache._from_my_logs() == {}

    def test_a_record_with_an_unparseable_date_is_dropped(self, tmp_path, monkeypatch):
        # The one behaviour that genuinely changed with the move to edi.read:
        # the old hand-parser accepted any line with 10+ fields and never
        # looked at the date, so a corrupt row still seeded the cache. The
        # strict variant wins, as elsewhere in this effort. Confirmed against
        # the old parser, which returns {"HA7NS": "JN97WM"} for this input.
        self._write(
            tmp_path, "a.edi", "NOTADATE;xxxx;HA7NS;2;599;001;599;001;;JN97WM;37;;;;\n"
        )
        monkeypatch.setattr(loc_cache, "MY_LOGS_DIR", tmp_path / "my-logs")
        assert loc_cache._from_my_logs() == {}

    def test_no_my_logs_directory_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loc_cache, "MY_LOGS_DIR", tmp_path / "nothing-here")
        assert loc_cache._from_my_logs() == {}


class TestMergeLocSources:
    def test_single_source_returned_unchanged(self):
        src = {"HA7NS": ["JN97WM", "JN97AB"]}
        assert loc_cache.merge_sources(src) == {"HA7NS": ["JN97WM", "JN97AB"]}

    def test_high_priority_loc_appears_first(self):
        # edi (high) > puskas (low)
        edi = {"HA7NS": ["JN97TF"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = loc_cache.merge_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97MM"]

    def test_three_sources_correct_order(self):
        edi = {"HA7NS": ["JN97TF"]}
        on4kst = {"HA7NS": ["JN97WM"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = loc_cache.merge_sources(edi, on4kst, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97WM", "JN97MM"]

    def test_duplicate_loc_kept_at_high_priority_position(self):
        # JN97TF appears in both edi and puskas; edi wins the position
        edi = {"HA7NS": ["JN97TF"]}
        puskas = {"HA7NS": ["JN97TF", "JN97MM"]}
        result = loc_cache.merge_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97MM"]

    def test_call_only_in_low_priority_source_is_included(self):
        edi = {"HA7NS": ["JN97TF"]}
        puskas = {"DL2ABC": ["JO50XY"]}
        result = loc_cache.merge_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF"]
        assert result["DL2ABC"] == ["JO50XY"]

    def test_empty_sources_return_empty(self):
        assert loc_cache.merge_sources({}, {}, {}) == {}

    def test_multi_loc_stations_preserve_internal_order(self):
        # on4kst has two locs for a station; both appear before puskas loc
        on4kst = {"HA7NS": ["JN97WM", "JN97AB"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = loc_cache.merge_sources(on4kst, puskas)
        assert result["HA7NS"] == ["JN97WM", "JN97AB", "JN97MM"]


# ──────────────────────────────────────────────────────────────
# loc_cache.remember
# ──────────────────────────────────────────────────────────────


class TestUpdateLocCache:
    def test_new_call_is_added(self):
        cache: dict = {}
        loc_cache.remember(cache, "HA7NS", "JN97WM")
        assert cache == {"HA7NS": ["JN97WM"]}

    def test_new_loc_inserted_at_front(self):
        cache = {"HA7NS": ["JN97WM"]}
        loc_cache.remember(cache, "HA7NS", "JN97TF")
        assert cache["HA7NS"] == ["JN97TF", "JN97WM"]

    def test_existing_loc_moved_to_front(self):
        cache = {"HA7NS": ["JN97WM", "JN97TF"]}
        loc_cache.remember(cache, "HA7NS", "JN97TF")
        assert cache["HA7NS"] == ["JN97TF", "JN97WM"]

    def test_loc_already_at_front_unchanged(self):
        cache = {"HA7NS": ["JN97WM", "JN97TF"]}
        loc_cache.remember(cache, "HA7NS", "JN97WM")
        assert cache["HA7NS"] == ["JN97WM", "JN97TF"]

    def test_empty_loc_ignored(self):
        cache = {"HA7NS": ["JN97WM"]}
        loc_cache.remember(cache, "HA7NS", "")
        assert cache["HA7NS"] == ["JN97WM"]


class TestIsContestTime:
    # First Monday of June 2026 = June 1, 18:00–19:59 CET (= UTC+2 in summer)
    def _t(self, y, mo, d, h, mi=0):
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)

    def test_during_contest(self):
        # 2026-06-01 is Monday; 18:00 CET = 16:00 UTC (CEST = UTC+2)
        assert _is_contest_time(self._t(2026, 6, 1, 16, 0)) is True

    def test_one_second_before_start(self):
        assert _is_contest_time(self._t(2026, 6, 1, 15, 59)) is False

    def test_at_end_boundary(self):
        # 20:00 CET = 18:00 UTC — contest is over
        assert _is_contest_time(self._t(2026, 6, 1, 18, 0)) is False

    def test_one_minute_before_end(self):
        assert _is_contest_time(self._t(2026, 6, 1, 17, 59)) is True

    def test_wrong_weekday(self):
        # 2026-06-02 is Tuesday
        assert _is_contest_time(self._t(2026, 6, 2, 16, 0)) is False

    def test_second_monday(self):
        # 2026-06-08 is the second Monday of June
        assert _is_contest_time(self._t(2026, 6, 8, 16, 0)) is False

    def test_winter_time(self):
        # First Monday of January 2026 = Jan 5; CET = UTC+1, so 18:00 CET = 17:00 UTC
        assert _is_contest_time(self._t(2026, 1, 5, 17, 0)) is True

    def test_winter_before_start(self):
        assert _is_contest_time(self._t(2026, 1, 5, 16, 59)) is False


class TestRpromptBearing:
    """Pin the bearing/distance math used by the rprompt.

    The rprompt was silently broken because initial_bearing was missing from
    puskas_logger — a NameError swallowed by 'except Exception: pass'.  These
    tests ensure the function exists here and returns correct values.
    """

    def test_initial_bearing_due_north(self):
        assert initial_bearing(0, 0, 10, 0) == pytest.approx(0.0, abs=1.0)

    def test_initial_bearing_due_east(self):
        assert initial_bearing(0, 0, 0, 10) == pytest.approx(90.0, abs=1.0)

    def test_initial_bearing_due_south(self):
        assert initial_bearing(10, 0, 0, 0) == pytest.approx(180.0, abs=1.0)

    def test_initial_bearing_due_west(self):
        assert initial_bearing(0, 10, 0, 0) == pytest.approx(270.0, abs=1.0)

    def test_rprompt_path_jn97_to_io83(self):
        # Full path from loc_cache lookup through maidenhead → dist+bearing,
        # the exact computation _rprompt does before returning the HTML string.
        my_loc = "JN97TF"
        his_loc = "IO83RO"
        lat1, lon1 = maidenhead_to_latlon(my_loc)
        lat2, lon2 = maidenhead_to_latlon(his_loc)
        dist = int(haversine_km(lat1, lon1, lat2, lon2))
        bear = int(initial_bearing(lat1, lon1, lat2, lon2))
        assert 1650 < dist < 1800  # roughly Budapest → Edinburgh
        assert 290 < bear < 320  # northwest


class TestBearingArrow:
    def test_north(self):
        assert _bearing_arrow(0) == "↑"

    def test_northeast(self):
        assert _bearing_arrow(45) == "↗"

    def test_east(self):
        assert _bearing_arrow(90) == "→"

    def test_southeast(self):
        assert _bearing_arrow(135) == "↘"

    def test_south(self):
        assert _bearing_arrow(180) == "↓"

    def test_southwest(self):
        assert _bearing_arrow(225) == "↙"

    def test_west(self):
        assert _bearing_arrow(270) == "←"

    def test_northwest(self):
        assert _bearing_arrow(315) == "↖"

    def test_boundary_wraps_to_north(self):
        assert _bearing_arrow(359) == "↑"

    def test_jn97_to_io83_is_northwest(self):
        # bearing ≈302°, which rounds to ↖ (NW octant 292.5–337.5)
        assert _bearing_arrow(302) == "↖"


class TestFormatCombos:
    def test_empty_dict_yields_empty_string(self):
        assert _format_combos({}) == ""

    def test_single_band(self):
        assert _format_combos({"70CM": ["CW", "FM"]}) == "70CM:CW,FM"

    def test_multiple_bands_space_separated_in_order(self):
        assert (
            _format_combos({"2M": ["CW"], "23CM": ["SSB", "CW", "FM"]})
            == "2M:CW 23CM:SSB,CW,FM"
        )


class TestWebcamCaptureCmd:
    def test_includes_device_audio_source_and_output(self):
        cmd = _webcam_capture_cmd("/dev/video2", "my_mic", "out.mp4")
        assert "/dev/video2" in cmd
        assert "my_mic" in cmd
        assert cmd[-1] == "out.mp4"

    def test_uses_v4l2_and_pulse(self):
        cmd = _webcam_capture_cmd("/dev/video0", "default", "out.mp4")
        assert "v4l2" in cmd
        assert "pulse" in cmd

    def test_stamps_v4l2_frames_with_wallclock(self):
        # -use_wallclock_as_timestamps 1 must be an *input* option on the v4l2
        # camera (before its -i), so ffmpeg tags each frame with the real
        # capture wallclock. This is what lets contest_video read an exact
        # frame-0 UTC start from the log, instead of the ~1s-early
        # webcam_start event (stamped before ffmpeg even spawns).
        cmd = _webcam_capture_cmd("/dev/video0", "default", "out.mp4")
        assert "-use_wallclock_as_timestamps" in cmd
        i = cmd.index("-use_wallclock_as_timestamps")
        assert cmd[i + 1] == "1"
        # before the camera's own -i /dev/video0 (an input option)
        assert i < cmd.index("/dev/video0") - 1


class TestWebcamPreciseStart:
    def test_parses_video_input_start_time(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text(
            "Input #0, video4linux2,v4l2, from '/dev/video0':\n"
            "  Duration: N/A, start: 1784722261.868307, bitrate: 147456 kb/s\n"
            "Input #1, pulse, from 'default':\n"
            "  Duration: N/A, start: 1784722261.854603, bitrate: 1536 kb/s\n"
        )
        assert _webcam_precise_start(str(log)) == datetime(
            2026, 7, 22, 12, 11, 1, 868307
        )

    def test_falls_back_to_audio_when_no_video_input(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text(
            "Input #0, pulse, from 'default':\n"
            "  Duration: N/A, start: 1784722261.854603, bitrate: 1536 kb/s\n"
        )
        assert _webcam_precise_start(str(log)) == datetime(
            2026, 7, 22, 12, 11, 1, 854603
        )

    def test_returns_none_when_no_start_line(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("ffmpeg version 7.1.5\nsome unrelated output\n")
        assert _webcam_precise_start(str(log)) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _webcam_precise_start(str(tmp_path / "nope.log")) is None

    def test_ignores_uptime_style_start_not_a_real_epoch(self, tmp_path):
        # Without -use_wallclock_as_timestamps, v4l2 reports CLOCK_MONOTONIC
        # (a small number, uptime) rather than a real epoch -- must not be
        # mistaken for one (real epochs are always > 1e9).
        log = tmp_path / "x.log"
        log.write_text(
            "Input #0, video4linux2,v4l2, from '/dev/video0':\n"
            "  Duration: N/A, start: 123.456789, bitrate: 147456 kb/s\n"
        )
        assert _webcam_precise_start(str(log)) is None


class TestWebcamPreciseName:
    def test_inserts_timestamp_before_extension(self):
        start = datetime(2026, 7, 22, 12, 11, 1, 868307)
        assert (
            _webcam_precise_name("foo-webcam.mp4", start)
            == "foo-webcam-20260722T121101.868307Z.mp4"
        )

    def test_preserves_directory_prefix(self):
        start = datetime(2026, 7, 22, 12, 11, 1, 868307)
        assert (
            _webcam_precise_name("/a/b/foo-webcam.mp4", start)
            == "/a/b/foo-webcam-20260722T121101.868307Z.mp4"
        )


class TestWebcamToggleRename:
    """webcam_toggle's stop branch, with a mocked ffmpeg process so this
    doesn't need real webcam hardware -- only the rename logic (the part
    this feature actually added) is under test."""

    def _run_stop(self, out_path, log_path):
        fake_proc = MagicMock()
        fake_proc.wait.return_value = 0
        recorders._webcam_proc = fake_proc
        recorders._webcam_out_path = str(out_path)
        recorders._webcam_log_path = str(log_path)
        recorders._webcam_log_fh = None
        try:
            return webcam_toggle("")
        finally:
            recorders._webcam_proc = None
            recorders._webcam_out_path = None
            recorders._webcam_log_path = None

    def test_renames_file_using_precise_log_timestamp(self, tmp_path):
        out_path = tmp_path / "prefix-webcam.mp4"
        out_path.write_bytes(b"fake mp4 data")
        log_path = tmp_path / "prefix-webcam.log"
        log_path.write_text(
            "Input #0, video4linux2,v4l2, from '/dev/video0':\n"
            "  Duration: N/A, start: 1784722261.868307, bitrate: 147456 kb/s\n"
        )
        msg = self._run_stop(out_path, log_path)
        assert msg == "recording stopped"
        assert not out_path.exists()
        renamed = tmp_path / "prefix-webcam-20260722T121101.868307Z.mp4"
        assert renamed.exists()
        assert renamed.read_bytes() == b"fake mp4 data"

    def test_leaves_file_unrenamed_when_log_has_no_start_time(self, tmp_path):
        out_path = tmp_path / "prefix-webcam.mp4"
        out_path.write_bytes(b"fake mp4 data")
        log_path = tmp_path / "prefix-webcam.log"
        log_path.write_text("no useful lines here\n")
        msg = self._run_stop(out_path, log_path)
        assert msg == "recording stopped"
        assert out_path.exists()
        assert out_path.read_bytes() == b"fake mp4 data"


# ──────────────────────────────────────────────────────────────
# rotator.current
# ──────────────────────────────────────────────────────────────


class TestCurrentRot:
    """rotator.current() reflects rotator._rot state; drives the ROT: toolbar segment."""

    def setup_method(self):
        with rotator._rot_lock:
            self._saved = dict(rotator._rot)

    def teardown_method(self):
        with rotator._rot_lock:
            rotator._rot.update(self._saved)

    def test_offline_when_not_connected(self):
        with rotator._rot_lock:
            rotator._rot.update(az=0.0, online=False)
        _, online = rotator.current()
        assert online is False

    def test_returns_azimuth_when_online(self):
        with rotator._rot_lock:
            rotator._rot.update(az=123.0, online=True)
        az, online = rotator.current()
        assert online is True
        assert az == pytest.approx(123.0)

    def test_azimuth_not_exposed_when_offline(self):
        # Toolbar shows ROT: --- when offline; az value must not be trusted.
        # rotator.current() signals this via online=False regardless of az content.
        with rotator._rot_lock:
            rotator._rot.update(az=270.0, online=False)
        _, online = rotator.current()
        assert online is False


# ──────────────────────────────────────────────────────────────
# Locator-only bearing path
# ──────────────────────────────────────────────────────────────


class TestLocatorOnlyBearing:
    """Pin the locator-only bearing lookup used for rotator pointing.

    When the operator types a bare locator (e.g. heard on air) as the only
    token in the input buffer, _rprompt shows bearing/distance and Alt+R turns
    the rotator there.  The path branches on is_locator(first) with
    len(tokens)==1 — the len guard prevents firing mid-QSO (e.g. "HA7NS JN97WM").
    lb.bearing()/lb.dist() accept a raw locator string directly; no cache lookup.
    """

    def test_typical_callsigns_do_not_read_as_locators(self):
        # Callsigns like HA7NS have a letter where position 3 must be a digit,
        # so they never match [A-R]{2}[0-9]{2}([A-X]{2})? — no false locator
        # trigger. geo's own tests cover the pattern itself; this pins the
        # property the rotator branch depends on.
        assert is_locator("JN97") and is_locator("JN97WM")
        assert not is_locator("HA7NS")
        assert not is_locator("DL2ABC")
        assert not is_locator("OE5XYZ")

    def test_bearing_from_raw_typed_locator(self):
        # lb.bearing() takes the locator string directly — no cache lookup needed.
        lb = LogBook("HA5LA", "JN97TF", {})
        bear = lb.bearing("IO83RO")
        dist = lb.dist("IO83RO")
        assert 290 < bear < 320  # northwest
        assert 1650 < dist < 1800


class TestTelemetryRecord:
    """Telemetry is change-driven now: a rig event whenever icom_net pushes a
    genuine freq/mode change, a rotator event whenever the polled azimuth
    actually moves. Each record carries only its own source's fields --
    contest_video.py carries the rest forward across events that don't
    mention them."""

    _T = datetime(2026, 7, 4, 9, 8, 15, 123456, tzinfo=timezone.utc)

    def test_rig_record(self):
        rec = telemetry_rig_record(self._T, 144174000, "CW")
        assert rec["t"] == "2026-07-04T09:08:15.123456Z"
        assert rec["freq_hz"] == 144174000
        assert rec["mode"] == "CW"
        assert "ptt" not in rec
        assert "az" not in rec  # a rig event says nothing about the rotator

    def test_rig_record_keeps_exact_hz(self):
        # Not the toolbar's kHz-rounded `qrg` string the old 1 Hz sampler
        # re-parsed: that rounding is exactly where contest_video's
        # documented 160 Hz "WAV vs telemetry disagreement" came from
        # (144299840 -> "144.300" -> 144300000), so it was this logger
        # quantizing, not two sources reading the rig differently.
        assert telemetry_rig_record(self._T, 144299840, "SSB")["freq_hz"] == 144299840

    def test_rig_offline_record_is_null_fields(self):
        rec = telemetry_rig_record(self._T, None, None)
        assert rec["freq_hz"] is None
        assert rec["mode"] is None

    def test_rot_record(self):
        rec = telemetry_rot_record(self._T, 135.04)
        assert rec["t"] == "2026-07-04T09:08:15.123456Z"
        assert rec["az"] == 135.0
        assert "freq_hz" not in rec  # nor a rotator event about the rig
        assert "mode" not in rec

    def test_rot_offline_record_is_null(self):
        assert telemetry_rot_record(self._T, None)["az"] is None


class TestInputLog:
    """Event-triggered input-box recorder feeding contest_video.py's
    typewriter overlay -- one JSON line per buffer change, not polled."""

    @pytest.fixture(autouse=True)
    def _reset_fh(self):
        yield
        if recorders._input_log_fh is not None:
            recorders._input_log_fh.close()
        recorders._input_log_fh = None

    class _Buf:
        def __init__(self, text):
            self.text = text

    def test_writes_a_line_per_change(self, tmp_path):
        import json

        path = tmp_path / "input.jsonl"
        input_log_open(path)
        on_buffer_changed(self._Buf("H"))
        on_buffer_changed(self._Buf("HA"))
        on_buffer_changed(self._Buf(""))  # Enter/Escape clears the buffer
        recorders._input_log_fh.close()
        lines = path.read_text().splitlines()
        assert [json.loads(line)["text"] for line in lines] == ["H", "HA", ""]

    def test_noop_before_a_log_is_opened(self):
        on_buffer_changed(self._Buf("X"))  # must not raise with no file open

    def test_record_has_microsecond_timestamp(self, tmp_path):
        import json

        path = tmp_path / "input.jsonl"
        input_log_open(path)
        on_buffer_changed(self._Buf("HA7NS"))
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["t"].endswith("Z")
        datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%S.%fZ")  # doesn't raise

    def test_keystroke_event_is_tagged_text(self, tmp_path):
        import json

        path = tmp_path / "input.jsonl"
        input_log_open(path)
        on_buffer_changed(self._Buf("HA7NS"))
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["event"] == "text"

    def test_log_input_event_writes_arbitrary_qso_record(self, tmp_path):
        # This is what the "New QSO" path in run() writes -- an explicit,
        # unambiguous marker distinct from the keystroke stream, since
        # submit vs. abort can't be told apart at the buffer-changed level.
        import json

        path = tmp_path / "input.jsonl"
        input_log_open(path)
        recorders.log_input_event(
            {
                "t": "2026-07-06T16:01:02.345678Z",
                "event": "qso",
                "call": "HA3KHB",
                "band": "2M",
                "mode": "CW",
                "nr_s": 3,
                "dup": False,
            }
        )
        recorders._input_log_fh.close()
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec == {
            "t": "2026-07-06T16:01:02.345678Z",
            "event": "qso",
            "call": "HA3KHB",
            "band": "2M",
            "mode": "CW",
            "nr_s": 3,
            "dup": False,
        }


class TestRadioUpdate:
    """icom_net push-update -> _rig cache (replaces the rigctld poller)."""

    def _reset(self):
        with _rig_lock:
            _rig.update(band="", mode="", qrg="", online=False)
        pl._rig_manual.update(band="", mode="")

    def test_partial_update_stays_offline(self):
        # connect() primes freq and mode with separate queries; until both
        # have arrived the rig must not report online (a half-known state
        # would show mode "SSB" by fallback for a moment).
        self._reset()
        pl._on_radio_update(144_174_000, None, "2M")
        assert pl.current_rig() == ("", "", "", False)

    def test_full_update_goes_online_with_formatted_qrg(self):
        self._reset()
        pl._on_radio_update(144_174_000, "USB", "2M")
        assert pl.current_rig() == ("2M", "SSB", "144.174", True)
        self._reset()

    def test_cw_reverse_mode_maps_to_cw(self):
        # icom_net spells reverse CW "CW-R" (rigctld spelled it "CWR").
        self._reset()
        pl._on_radio_update(144_050_000, "CW-R", "2M")
        assert pl.current_rig() == ("2M", "CW", "144.050", True)
        self._reset()


class TestRigServer:
    """The logger's rigctld-dialect TCP server (port 4532) -- what
    on4kst_irc_bridge.py polls now that rigctld itself is gone."""

    def _client(self, port):
        import socket

        s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        return s, s.makefile("rb")

    def _serve(self):
        import threading

        srv = rig_server.bind(0)
        assert srv is not None
        threading.Thread(
            target=rig_server.serve, args=(srv, pl.rig_snapshot), daemon=True
        ).start()
        return srv, srv.getsockname()[1]

    def test_serves_f_and_m_like_rigctld(self):
        # Exactly the byte flow on4kst_irc_bridge.fetch_rig_info() produces:
        # "f\nm\n", then read freq line + mode line (passband line ignored).
        with _rig_lock:
            _rig.update(
                band="2M",
                mode="CW",
                qrg="144.080",
                online=True,
                freq_hz=144_080_000,
                raw_mode="CW",
            )
        srv, port = self._serve()
        try:
            s, r = self._client(port)
            s.sendall(b"f\nm\n")
            assert r.readline() == b"144080000\n"
            assert r.readline() == b"CW\n"
            assert r.readline() == b"0\n"
            s.close()
        finally:
            srv.close()
            with _rig_lock:
                _rig.update(band="", mode="", qrg="", online=False)

    def test_replies_rprt_error_when_radio_offline(self):
        with _rig_lock:
            _rig.update(band="", mode="", qrg="", online=False)
        srv, port = self._serve()
        try:
            s, r = self._client(port)
            s.sendall(b"f\n")
            assert r.readline() == b"RPRT -1\n"
            s.close()
        finally:
            srv.close()

    def test_bind_yields_none_when_port_taken(self):
        srv = rig_server.bind(0)
        assert srv is not None
        try:
            assert rig_server.bind(srv.getsockname()[1]) is None
        finally:
            srv.close()


class TestScopeRecorder:
    def test_on_scope_appends_records_readable_by_icom_net(self, tmp_path):
        import icom_net

        path = tmp_path / "test.scope"
        recorders._scope_rec["path"] = path
        try:
            recorders.on_scope(145_000_000, 146_000_000, bytes(range(100)))
            recorders.on_scope(432_000_000, 433_000_000, bytes(100))
            records = icom_net.read_scope_records(path)
            assert len(records) == 2
            ts0, start0, end0, px0 = records[0]
            assert (start0, end0, px0) == (145_000_000, 146_000_000, bytes(range(100)))
            _, start1, end1, px1 = records[1]
            assert (start1, end1, px1) == (432_000_000, 433_000_000, bytes(100))
            assert ts0 > 0
        finally:
            if recorders._scope_rec["file"] is not None:
                recorders._scope_rec["file"].close()
            recorders._scope_rec.update(path=None, file=None)

    def test_on_scope_is_a_noop_without_a_configured_path(self):
        recorders.on_scope(145_000_000, 146_000_000, b"\x01\x02")
        assert recorders._scope_rec["file"] is None


def test_radio_close_if_connected_closes_and_clears_the_session():
    # A logger exit must deregister the radio session (IcomNetRig.close
    # sends the token deregister) so a restart never races the radio's
    # abandoned-session cooldown.
    rig = MagicMock()
    with _rig_lock:
        pl._radio["rig"] = rig
    try:
        pl._radio_close_if_connected()
        rig.close.assert_called_once()
        assert pl._radio["rig"] is None
    finally:
        with _rig_lock:
            pl._radio["rig"] = None
