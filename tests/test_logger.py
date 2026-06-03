"""Tests for puskas_logger pure functions — no rig, no network, no prompts."""
import io
from datetime import datetime, timezone

import pytest

from puskas_logger import (
    QSO,
    LogBook,
    _band_summary,
    _bearing_arrow,
    _edi_qso_count,
    _is_contest_time,
    _is_dup_in_log,
    _merge_loc_sources,
    _predict_nr,
    _print_recent,
    _update_loc_cache,
    haversine_km,
    initial_bearing,
    load_from_edi,
    maidenhead_to_latlon,
    parse_input,
    tname_for,
    write_edi,
)

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _dt(h: int = 16, m: int = 0) -> datetime:
    return datetime(2026, 5, 4, h, m, tzinfo=timezone.utc)

def _qso(call="HA7NS", band="2M", mode="SSB", nr_s=1, nr_r=1,
         rst_s="59", rst_r="59", loc="JN97WM", dist_km=38,
         h=16, m=0, dt=None) -> QSO:
    return QSO(dt=dt or _dt(h, m), band=band, mode=mode, call=call,
               rst_s=rst_s, nr_s=nr_s, rst_r=rst_r, nr_r=nr_r,
               loc=loc, dist_km=dist_km)


# ──────────────────────────────────────────────────────────────
# parse_input
# ──────────────────────────────────────────────────────────────

class TestParseInput:
    def test_three_tokens_no_locator_returns_error(self):
        r = parse_input("HA7NS 59 015")
        assert "Usage" in r

    def test_with_locator(self):
        r = parse_input("HA7NS 59 015 JN97WM")
        assert r == {"call": "HA7NS", "rst_r": "59", "nr_r": 15, "loc": "JN97WM"}

    def test_cw_rst(self):
        r = parse_input("HA7NS 599 014 JN97WM")
        assert r == {"call": "HA7NS", "rst_r": "599", "nr_r": 14, "loc": "JN97WM"}

    def test_lowercase_input_normalised(self):
        r = parse_input("ha7ns 59 001 jn97wm")
        assert r["call"] == "HA7NS"
        assert r["loc"] == "JN97WM"

    def test_four_char_locator_accepted(self):
        r = parse_input("HA7NS 59 001 JN97")
        assert r["loc"] == "JN97"

    def test_eight_char_locator_not_accepted(self):
        # RE_LOC is anchored — 8-char string doesn't match, so loc stays empty → error
        r = parse_input("HA7NS 59 001 JN97WMXX")
        assert "Usage" in r

    def test_portable_callsign(self):
        r = parse_input("HA5LA/P 59 007 JN97TF")
        assert r["call"] == "HA5LA/P"

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
        self.lb.add(_qso(band="2M",   nr_s=1))
        self.lb.add(_qso(band="70CM", nr_s=1))
        assert self.lb.next_nr("") == 3

    def test_add_returns_false_for_new_qso(self):
        assert self.lb.add(_qso()) is False

    def test_add_returns_true_for_dup(self):
        self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB"))
        assert self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB")) is True

    def test_dup_check_uses_all_three_keys(self):
        self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB"))
        assert self.lb.add(_qso(call="HA7NS", band="70CM", mode="SSB")) is False
        assert self.lb.add(_qso(call="HA7NS", band="2M",   mode="CW"))  is False
        assert self.lb.add(_qso(call="HA7NS", band="2M",   mode="FM"))  is False

    def test_nine_valid_combos_per_station(self):
        combos = [(b, m) for b in ("2M", "70CM", "23CM")
                         for m in ("SSB", "CW", "FM")]
        assert len(combos) == 9
        for b, m in combos:
            assert self.lb.add(_qso(call="HA7NS", band=b, mode=m)) is False

    def test_undo_removes_last_qso(self):
        self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_s=1))
        self.lb.add(_qso(call="HA3KHB", band="2M", mode="SSB", nr_s=2))
        q = self.lb.undo()
        assert q.call == "HA3KHB"
        assert len(self.lb.qsos) == 1

    def test_undo_rebuilds_worked_set(self):
        self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB"))
        self.lb.undo()
        assert self.lb.add(_qso(call="HA7NS", band="2M", mode="SSB")) is False

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
        q = _qso(call="HA7NS", band="2M", mode="SSB")
        assert _is_dup_in_log([q], q) is False

    def test_second_occurrence_is_dup(self):
        q1 = _qso(call="HA7NS", band="2M", mode="SSB", h=16)
        q2 = _qso(call="HA7NS", band="2M", mode="SSB", h=17)
        assert _is_dup_in_log([q1, q2], q2) is True

    def test_different_band_not_dup(self):
        q1 = _qso(call="HA7NS", band="2M",   mode="SSB")
        q2 = _qso(call="HA7NS", band="70CM", mode="SSB")
        assert _is_dup_in_log([q1, q2], q2) is False


# ──────────────────────────────────────────────────────────────
# tname_for
# ──────────────────────────────────────────────────────────────

class TestTnameFor:
    def test_may_2026(self):
        assert tname_for(datetime(2026, 5, 4, tzinfo=timezone.utc)) == "PUSKAS2026MAJUS"

    def test_january(self):
        assert tname_for(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "PUSKAS2026JANUAR"

    def test_december(self):
        assert tname_for(datetime(2025, 12, 8, tzinfo=timezone.utc)) == "PUSKAS2025DECEMBER"


# ──────────────────────────────────────────────────────────────
# write_edi
# ──────────────────────────────────────────────────────────────

class TestWriteEdi:
    def setup_method(self, tmp_path_factory):
        # Use pytest's tmp_path fixture via a workaround; tests use self.tmp_path
        pass

    def test_writes_file(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_s=1, nr_r=1, dist_km=38))
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
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB",
                    nr_s=1, nr_r=1, rst_s="59", rst_r="59", loc="JN97WM", dist_km=38))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "260504;1600;HA7NS;1;59;001;59;001;;JN97WM;38" in txt

    def test_cw_mode_code(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", mode="CW", rst_s="599", rst_r="599", nr_s=1))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert ";2;" in txt   # mode code 2 = CW

    def test_fm_mode_code(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", mode="FM", nr_s=1))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert ";6;" in txt   # mode code 6 = FM

    def test_dup_flagged_with_d(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_s=1, h=16))
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_s=2, h=17))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        lines = [line for line in txt.splitlines() if "HA7NS" in line]
        assert len(lines) == 2
        assert lines[0].endswith(";")    # not a dup
        assert lines[1].endswith("D;")  # dup

    def test_dup_excluded_from_score(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS",  band="2M", mode="SSB", nr_s=1, dist_km=38, h=16))
        lb.add(_qso(call="HA3KHB", band="2M", mode="SSB", nr_s=2, dist_km=168, h=17))
        lb.add(_qso(call="HA7NS",  band="2M", mode="SSB", nr_s=3, dist_km=38,  h=18))
        txt = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path).read_text()
        assert "CQSOP=206" in txt   # 38 + 168; dup not counted

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
        lb.add(_qso(call="HA7NS",  band="2M", mode="SSB", nr_s=1, nr_r=1,  dist_km=38,  h=16, m=1))
        lb.add(_qso(call="HA3KHB", band="2M", mode="CW",  nr_s=2, nr_r=14, dist_km=168, h=16, m=59,
                    rst_s="599", rst_r="599"))
        lb.add(_qso(call="HA7NS",  band="2M", mode="SSB", nr_s=3, nr_r=2,  dist_km=38,  h=17, m=5))
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
        assert lb2.my_call == "HA5LA"
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
        assert lb2.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_s=4)) is True

    def test_roundtrip_next_nr_continues(self, tmp_path):
        lb = self._make_logbook()
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert lb2.next_nr("2M") == 4

    def test_multiband_roundtrip(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS", band="2M",   mode="SSB", nr_s=1, h=16))
        lb.add(_qso(call="HA7NS", band="70CM", mode="FM",  nr_s=1, h=17))
        write_edi(lb, "2M",   "PUSKAS2026MAJUS", tmp_path)
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
        assert lb2.qsos[0].call == "HA7NS"

    def test_uppercase_and_lowercase_edi_not_doubled(self, tmp_path):
        """Coexisting foo.EDI and foo.edi (case-change migration) must not double QSOs."""
        lb = self._make_logbook()
        p = write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        # Simulate a stale uppercase sibling from a pre-lowercase-change save
        stale = p.with_suffix(".EDI")
        stale.write_text(p.read_text())
        paths = sorted(tmp_path.glob("*.[Ee][Dd][Ii]"))
        assert len(paths) == 2          # both files visible on Linux
        lb2, _ = load_from_edi(paths, {})
        assert len(lb2.qsos) == len(lb.qsos)   # no doubling

    def test_write_edi_removes_uppercase_sibling(self, tmp_path):
        lb = self._make_logbook()
        # Create a stale uppercase file first
        stale = tmp_path / "260504-HA5LA-2M.EDI"
        stale.write_text("stale")
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        assert not stale.exists()

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
        lb.add(_qso(call="HA7NS",  band="2M", mode="SSB", nr_s=1, nr_r=1,  h=16, m=1))
        lb.add(_qso(call="HA3KHB", band="2M", mode="SSB", nr_s=2, nr_r=14, h=16, m=59))
        lb.add(_qso(call="HA8RM",  band="2M", mode="SSB", nr_s=3, nr_r=12, h=17, m=4))
        return lb

    def _apply_edit(self, lb, edit_idx, parsed):
        real_idx = len(lb.qsos) - 1 - edit_idx
        old = lb.qsos[real_idx]
        loc = parsed["loc"]  # mandatory
        lb.qsos[real_idx] = QSO(
            dt=old.dt, band=old.band, mode=old.mode,
            call=parsed["call"], rst_s=old.rst_s, nr_s=old.nr_s,
            rst_r=parsed["rst_r"], nr_r=parsed["nr_r"],
            loc=loc, dist_km=lb.dist(loc),
        )
        lb.worked = {(q.call, q.band, q.mode) for q in lb.qsos}

    def test_edit_last_callsign_typo(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA8RM 59 012 JN96UW")
        # edit_idx=0 → last QSO (HA8RM was logged with wrong loc, fix it)
        self._apply_edit(lb, 0, parsed)
        assert lb.qsos[2].call == "HA8RM"
        assert lb.qsos[2].loc == "JN96UW"

    def test_edit_preserves_dt_band_mode_nr_s_rst_s(self):
        lb = self._lb_with_qsos()
        original = lb.qsos[0]
        parsed = parse_input("HA7NS 59 002 JN97WM")
        self._apply_edit(lb, 2, parsed)   # edit_idx=2 → first QSO
        edited = lb.qsos[0]
        assert edited.dt    == original.dt
        assert edited.band  == original.band
        assert edited.mode  == original.mode
        assert edited.nr_s  == original.nr_s
        assert edited.rst_s == original.rst_s

    def test_edit_middle_qso(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA3KHB 59 015 JN86SR")
        self._apply_edit(lb, 1, parsed)   # edit_idx=1 → middle QSO
        assert lb.qsos[1].nr_r == 15
        assert lb.qsos[0].call == "HA7NS"   # others unchanged
        assert lb.qsos[2].call == "HA8RM"

    def test_edit_rebuilds_worked_set(self):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA5OO 59 012 JN96UW")
        self._apply_edit(lb, 0, parsed)    # replace HA8RM with HA5OO
        assert ("HA5OO", "2M", "SSB") in lb.worked
        assert ("HA8RM", "2M", "SSB") not in lb.worked

    def test_edit_fixes_callsign_dup_detection(self):
        lb = self._lb_with_qsos()
        # HA7NS is in worked; edit first QSO to change its callsign
        parsed = parse_input("HA5OO 59 001 JN96UW")
        self._apply_edit(lb, 2, parsed)    # edit_idx=2 → first QSO
        # HA7NS should no longer be in worked (it was the only one)
        assert ("HA7NS", "2M", "SSB") not in lb.worked
        # Adding HA7NS now should not be a dup
        assert lb.add(_qso(call="HA7NS", band="2M", mode="SSB")) is False

    def test_missing_locator_returns_error(self):
        r = parse_input("HA7NS 59 001")
        assert "Usage" in r

    def test_edit_roundtrip_via_edi(self, tmp_path):
        lb = self._lb_with_qsos()
        parsed = parse_input("HA8RM 59 012 JN96UW")
        self._apply_edit(lb, 0, parsed)
        write_edi(lb, "2M", "PUSKAS2026MAJUS", tmp_path)
        lb2, _ = load_from_edi(list(tmp_path.glob("*.[Ee][Dd][Ii]")), {})
        assert lb2.qsos[2].call == "HA8RM"
        assert lb2.qsos[2].loc  == "JN96UW"


# ──────────────────────────────────────────────────────────────
# _band_summary
# ──────────────────────────────────────────────────────────────

class TestBandSummary:
    def test_no_qsos(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        assert _band_summary(lb) == "no QSOs yet"

    def test_single_band(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M", dist_km=100, nr_s=1, h=16))
        assert _band_summary(lb) == "2M:1q/100pt"

    def test_dups_excluded_from_pts(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS", band="2M", dist_km=100, nr_s=1, h=16))
        lb.add(_qso(call="HA7NS", band="2M", dist_km=100, nr_s=2, h=17))  # dup
        assert _band_summary(lb) == "2M:2q/100pt"

    def test_three_bands(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(band="2M",   dist_km=100, nr_s=1, h=16))
        lb.add(_qso(band="70CM", dist_km=200, nr_s=1, h=17))
        lb.add(_qso(band="23CM", dist_km=50,  nr_s=1, h=18))
        s = _band_summary(lb)
        assert "2M:1q/100pt" in s
        assert "70CM:1q/200pt" in s
        assert "23CM:1q/50pt" in s

    def test_fits_in_header_width(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        for i, (band, km) in enumerate([("2M", 9999), ("70CM", 9999), ("23CM", 9999)], 1):
            lb.add(_qso(band=band, dist_km=km, nr_s=i, h=16 + i))
        prefix = " PUSKÁS LOGGER  │  "
        full = prefix + _band_summary(lb)
        assert len(full) <= 64


# ──────────────────────────────────────────────────────────────
# _print_recent
# ──────────────────────────────────────────────────────────────

class TestPrintRecent:
    def _lb(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        for i in range(10):
            lb.add(_qso(call=f"HA{i}AA", nr_s=i + 1, h=14, m=i * 5, dist_km=100 + i))
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
        assert "HA9AA" in data[-1]   # last QSO at bottom

    def test_focus_row_has_arrow_prefix(self):
        lb = self._lb()
        focus = 5   # 6th QSO (0-indexed)
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
        focus = 3   # middle of log
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
        lb.add(_qso(call="HA7NS", nr_s=1, h=14, loc="JN97WM", dist_km=lb.dist("JN97WM")))
        lines = self._lines(lb, n=4)
        qso_line = next(line for line in lines if "HA7NS" in line)
        assert "°" in qso_line
        assert "km" in qso_line
        # bearing arrow follows "°" — check the char right after the degree sign + space
        deg_pos = qso_line.index("°")
        assert qso_line[deg_pos + 2] in "↑↗→↘↓↙←↖"

    def test_tx_rx_arrows_in_log_line(self):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS", nr_s=1, h=14, loc="JN97WM", dist_km=lb.dist("JN97WM")))
        lines = self._lines(lb, n=4)
        qso_line = next(line for line in lines if "HA7NS" in line)
        # ↑ labels the sent RST/NR, ↓ labels the received RST/NR
        assert "↑" in qso_line
        assert "↓" in qso_line
        # ↑ must come before ↓
        assert qso_line.index("↑") < qso_line.index("↓")

    def test_multiband_load_sorted_by_timestamp(self, tmp_path):
        lb = LogBook("HA5LA", "JN97TF", {})
        lb.add(_qso(call="HA7NS",  band="2M",   mode="SSB", nr_s=1, h=14, m=0))
        lb.add(_qso(call="HA3KHB", band="70CM",  mode="FM",  nr_s=1, h=14, m=10))
        lb.add(_qso(call="HA8RM",  band="2M",   mode="SSB", nr_s=2, h=14, m=20))
        write_edi(lb, "2M",   "T", tmp_path)
        write_edi(lb, "70CM", "T", tmp_path)
        # Load in 70CM-first order to exercise sorting
        paths = sorted(tmp_path.glob("*.[Ee][Dd][Ii]"), reverse=True)
        lb2, _ = load_from_edi(paths, {})
        assert [q.call for q in lb2.qsos] == ["HA7NS", "HA3KHB", "HA8RM"]


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
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) == 16

    def test_same_mode_not_used(self):
        lb = self._lb()
        lb.add(_qso(call="HA7NS", band="2M", mode="CW", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) is None

    def test_different_band_not_used(self):
        lb = self._lb()
        lb.add(_qso(call="HA7NS", band="70CM", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 4)) is None

    def test_most_recent_cross_mode_wins(self):
        lb = self._lb()
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_r=10, dt=_dt(16, 0)))
        lb.add(_qso(call="HA7NS", band="2M", mode="CW",  nr_r=20, dt=_dt(16, 1)))
        # current mode FM, 4 min later: most recent cross-mode is CW/20 → predict 21
        assert _predict_nr(lb, "HA7NS", "2M", "FM", now=_dt(16, 4)) == 21

    def test_old_qso_returns_none(self):
        lb = self._lb()
        lb.add(_qso(call="HA7NS", band="2M", mode="SSB", nr_r=15, dt=_dt(16, 0)))
        # 6 minutes later — outside the 5-minute window
        assert _predict_nr(lb, "HA7NS", "2M", "CW", now=_dt(16, 6)) is None


# ──────────────────────────────────────────────────────────────
# _merge_loc_sources
# ──────────────────────────────────────────────────────────────

class TestMergeLocSources:
    def test_single_source_returned_unchanged(self):
        src = {"HA7NS": ["JN97WM", "JN97AB"]}
        assert _merge_loc_sources(src) == {"HA7NS": ["JN97WM", "JN97AB"]}

    def test_high_priority_loc_appears_first(self):
        # edi (high) > puskas (low)
        edi    = {"HA7NS": ["JN97TF"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = _merge_loc_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97MM"]

    def test_three_sources_correct_order(self):
        edi    = {"HA7NS": ["JN97TF"]}
        on4kst = {"HA7NS": ["JN97WM"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = _merge_loc_sources(edi, on4kst, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97WM", "JN97MM"]

    def test_duplicate_loc_kept_at_high_priority_position(self):
        # JN97TF appears in both edi and puskas; edi wins the position
        edi    = {"HA7NS": ["JN97TF"]}
        puskas = {"HA7NS": ["JN97TF", "JN97MM"]}
        result = _merge_loc_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF", "JN97MM"]

    def test_call_only_in_low_priority_source_is_included(self):
        edi    = {"HA7NS": ["JN97TF"]}
        puskas = {"DL2ABC": ["JO50XY"]}
        result = _merge_loc_sources(edi, puskas)
        assert result["HA7NS"] == ["JN97TF"]
        assert result["DL2ABC"] == ["JO50XY"]

    def test_empty_sources_return_empty(self):
        assert _merge_loc_sources({}, {}, {}) == {}

    def test_multi_loc_stations_preserve_internal_order(self):
        # on4kst has two locs for a station; both appear before puskas loc
        on4kst = {"HA7NS": ["JN97WM", "JN97AB"]}
        puskas = {"HA7NS": ["JN97MM"]}
        result = _merge_loc_sources(on4kst, puskas)
        assert result["HA7NS"] == ["JN97WM", "JN97AB", "JN97MM"]


# ──────────────────────────────────────────────────────────────
# _update_loc_cache
# ──────────────────────────────────────────────────────────────

class TestUpdateLocCache:
    def test_new_call_is_added(self):
        cache: dict = {}
        _update_loc_cache(cache, "HA7NS", "JN97WM")
        assert cache == {"HA7NS": ["JN97WM"]}

    def test_new_loc_inserted_at_front(self):
        cache = {"HA7NS": ["JN97WM"]}
        _update_loc_cache(cache, "HA7NS", "JN97TF")
        assert cache["HA7NS"] == ["JN97TF", "JN97WM"]

    def test_existing_loc_moved_to_front(self):
        cache = {"HA7NS": ["JN97WM", "JN97TF"]}
        _update_loc_cache(cache, "HA7NS", "JN97TF")
        assert cache["HA7NS"] == ["JN97TF", "JN97WM"]

    def test_loc_already_at_front_unchanged(self):
        cache = {"HA7NS": ["JN97WM", "JN97TF"]}
        _update_loc_cache(cache, "HA7NS", "JN97WM")
        assert cache["HA7NS"] == ["JN97WM", "JN97TF"]

    def test_empty_loc_ignored(self):
        cache = {"HA7NS": ["JN97WM"]}
        _update_loc_cache(cache, "HA7NS", "")
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
        my_loc   = "JN97TF"
        his_loc  = "IO83RO"
        lat1, lon1 = maidenhead_to_latlon(my_loc)
        lat2, lon2 = maidenhead_to_latlon(his_loc)
        dist = int(haversine_km(lat1, lon1, lat2, lon2))
        bear = int(initial_bearing(lat1, lon1, lat2, lon2))
        assert 1650 < dist < 1800   # roughly Budapest → Edinburgh
        assert 290 < bear < 320     # northwest


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
