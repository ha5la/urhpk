"""Unit tests for parsing functions and regexes — no async, no network."""
import html

import pytest

from on4kst_irc_bridge import (
    RE_CHAT_MSG, RE_RECIPIENT, RE_USR, strip_iac,
    maidenhead_to_latlon, haversine_km, initial_bearing, sked_text,
)


class TestStripIAC:
    def test_passthrough(self):
        data = b"Hello\r\n"
        assert strip_iac(data) == data

    def test_removes_three_byte_sequence(self):
        assert strip_iac(b"pre\xFF\xFB\x01post") == b"prepost"

    def test_removes_multiple_sequences(self):
        assert strip_iac(b"\xFF\xFB\x01\xFF\xFD\x03ok") == b"ok"

    def test_truncated_iac_not_removed(self):
        # Only 2 bytes — not a complete IAC sequence, should be kept
        result = strip_iac(b"\xFF\xFB")
        assert b"\xFF" in result


class TestREUSR:
    def test_normal_entry(self):
        m = RE_USR.match("CT1FCX           IM59LG Pedro")
        assert m is not None
        assert m.group(1) == ""             # not away
        assert m.group(2).upper() == "CT1FCX"
        assert m.group(3).upper() == "IM59LG"
        assert "Pedro" in m.group(4)

    def test_away_entry(self):
        m = RE_USR.match("(DD0VF)          JO61TB Steffen 2-70-23")
        assert m is not None
        assert m.group(1) == "("            # away marker
        assert m.group(2).upper() == "DD0VF"
        assert m.group(3).upper() == "JO61TB"
        assert "Steffen" in m.group(4)

    def test_no_info(self):
        m = RE_USR.match("DF7KF            JO30FK")
        assert m is not None
        assert m.group(2).upper() == "DF7KF"
        assert m.group(3).upper() == "JO30FK"
        assert m.group(4).strip() == ""

    def test_portable_callsign(self):
        m = RE_USR.match("HA5LA/P          JN97MX Mobile")
        assert m is not None
        assert m.group(2).upper() == "HA5LA/P"

    def test_equipment_field(self):
        m = RE_USR.match("DK5DV            JO30XS Gerd 144/432 MHz")
        assert m is not None
        assert "144/432" in m.group(4)

    def test_no_match_for_chat_line(self):
        assert RE_USR.match("0712Z G6DDN Ian 2m14> Hello") is None


class TestREChatMsg:
    def test_public_message(self):
        line = "0712Z G6DDN Ian 2m14> Hello everyone"
        m = RE_CHAT_MSG.match(line)
        assert m is not None
        assert m.group(1) == "0712Z"
        assert m.group(2).upper() == "G6DDN"
        assert m.group(3) == "Hello everyone"

    def test_addressed_message(self):
        line = "0712Z S51AT Boris 6&2m> (PA0LMA) must go out"
        m = RE_CHAT_MSG.match(line)
        assert m is not None
        rest = m.group(3)
        r = RE_RECIPIENT.match(rest)
        assert r is not None
        assert r.group(1).upper() == "PA0LMA"
        assert "must go out" in r.group(2)

    def test_special_chars_in_name(self):
        # After html.unescape: &#9889; → ⚡
        line = "0713Z PA0LMA Hennie⚡2m> (S51AT) No prob."
        m = RE_CHAT_MSG.match(line)
        assert m is not None
        assert m.group(2).upper() == "PA0LMA"

    def test_no_match_for_user_list_line(self):
        assert RE_CHAT_MSG.match("CT1FCX           IM59LG Pedro") is None


class TestHTMLEntities:
    def test_comet(self):
        assert html.unescape("&#9732;") == "☄"

    def test_lightning(self):
        assert html.unescape("&#9889;") == "⚡"

    def test_ampersand(self):
        assert html.unescape("&amp;") == "&"
        assert html.unescape("6&amp;2m") == "6&2m"


class TestLocatorMath:
    def test_maidenhead_center_4char(self):
        lat, lon = maidenhead_to_latlon("JN97")
        assert abs(lat - 47.5) < 0.01
        assert abs(lon - 19.0) < 0.01

    def test_maidenhead_center_6char(self):
        lat, lon = maidenhead_to_latlon("JN97MX")
        assert 47.0 < lat < 48.5
        assert 18.5 < lon < 19.5

    def test_haversine_same_point(self):
        assert haversine_km(47.5, 19.0, 47.5, 19.0) == pytest.approx(0.0, abs=0.01)

    def test_haversine_known_range(self):
        lat1, lon1 = maidenhead_to_latlon("JN97")
        lat2, lon2 = maidenhead_to_latlon("IO83")
        d = haversine_km(lat1, lon1, lat2, lon2)
        assert 1400 < d < 1700  # JN97→IO83 ≈ 1550 km

    def test_bearing_due_north(self):
        assert abs(initial_bearing(0, 0, 10, 0)) < 1.0

    def test_bearing_due_east(self):
        assert abs(initial_bearing(0, 0, 0, 10) - 90.0) < 1.0

    def test_bearing_due_south(self):
        assert abs(initial_bearing(10, 0, 0, 0) - 180.0) < 1.0

    def test_bearing_jn97_to_io83_is_northwest(self):
        lat1, lon1 = maidenhead_to_latlon("JN97")
        lat2, lon2 = maidenhead_to_latlon("IO83")
        b = initial_bearing(lat1, lon1, lat2, lon2)
        assert 280 < b < 330  # northwest


class TestSkedText:
    def test_full_info(self):
        text = sked_text("G6DDN", "HA5LA", "JN97MX", "IO83RJ", ["2M"])
        assert "G6DDN" in text
        assert "HA5LA" in text
        assert "JN97MX" in text
        assert "2M" in text
        assert "km" in text
        assert "sked?" in text

    def test_no_locators(self):
        text = sked_text("G6DDN", "HA5LA", "", "", ["2M"])
        assert "G6DDN" in text
        assert "km" not in text

    def test_no_bands(self):
        text = sked_text("G6DDN", "HA5LA", "JN97MX", "IO83RJ", [])
        assert "km" in text
        assert "2M" not in text

    def test_multiple_bands_sorted(self):
        text = sked_text("G6DDN", "HA5LA", "JN97MX", "IO83RJ", ["70CM", "2M"])
        assert "2M" in text
        assert "70CM" in text
        assert text.index("2M") < text.index("70CM")  # sorted
