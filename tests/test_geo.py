"""Tests for geo, and for the None-handling its callers rely on.

The three formulas used to be copied into the logger, the bridge and the video
tool, and the copies disagreed on what a bad locator does: the video returned
None, the other two raised and were wrapped in `except Exception`. The strict
variant won, so these pin the behaviour the blanket excepts used to provide.
"""

import pytest

import on4kst_irc_bridge as bridge
import puskas_logger as pl
from geo import (
    bearing_between,
    distance_between,
    haversine_km,
    initial_bearing,
    latlon_to_maidenhead,
    maidenhead_to_latlon,
)

HOME = "JN97WM"  # 47.5208 N, 19.8750 E
FAR = "IO91"  # 51.5 N, 1.0 W


class TestLocatorParsing:
    def test_six_character(self):
        lat, lon = maidenhead_to_latlon(HOME)
        assert lat == pytest.approx(47.5208, abs=0.001)
        assert lon == pytest.approx(19.8750, abs=0.001)

    def test_four_character_is_square_centre(self):
        lat, lon = maidenhead_to_latlon("JN97")
        assert lat == pytest.approx(47.5, abs=0.01)
        assert lon == pytest.approx(19.0, abs=0.01)

    def test_lowercase_and_padding_accepted(self):
        assert maidenhead_to_latlon("  jn97wm  ") == maidenhead_to_latlon(HOME)

    @pytest.mark.parametrize(
        "bad", ["", "   ", "JN", "JN9", "ZZ99", "JN97WMXX", "1N97WM", "JN97W", "JNXXWM"]
    )
    def test_rejected(self, bad):
        assert maidenhead_to_latlon(bad) is None

    def test_none_input(self):
        assert maidenhead_to_latlon(None) is None

    def test_roundtrip(self):
        lat, lon = maidenhead_to_latlon(HOME)
        assert latlon_to_maidenhead(lat, lon) == HOME


class TestPairLevel:
    def test_distance(self):
        assert distance_between(HOME, FAR) == pytest.approx(1564.6, abs=1.0)

    def test_bearing_is_not_its_reverse(self):
        there = bearing_between(HOME, FAR)
        back = bearing_between(FAR, HOME)
        assert abs(((there - back) % 360) - 180) > 1.0

    @pytest.mark.parametrize("a,b", [("", FAR), (HOME, ""), ("ZZ99", FAR)])
    def test_none_when_either_side_unparseable(self, a, b):
        assert distance_between(a, b) is None
        assert bearing_between(a, b) is None


class TestBearingCardinals:
    @pytest.mark.parametrize(
        "args,expected",
        [
            ((0, 0, 10, 0), 0.0),
            ((0, 0, 0, 10), 90.0),
            ((10, 0, 0, 0), 180.0),
            ((0, 10, 0, 0), 270.0),
        ],
    )
    def test_cardinal(self, args, expected):
        assert initial_bearing(*args) == pytest.approx(expected, abs=1.0)

    def test_haversine_symmetric(self):
        a = maidenhead_to_latlon(HOME)
        b = maidenhead_to_latlon(FAR)
        assert haversine_km(*a, *b) == pytest.approx(haversine_km(*b, *a))


class TestCallersSurviveBadLocators:
    """What the deleted `except Exception:` blocks used to guarantee."""

    @pytest.mark.parametrize("bad", ["", "ZZ99", "NOTALOC"])
    def test_logbook_dist_and_bearing_return_zero(self, bad):
        lb = pl.LogBook("HA5LA", HOME, {})
        assert lb.dist(bad) == 0
        assert lb.bearing(bad) == 0

    def test_logbook_with_bad_own_locator(self):
        lb = pl.LogBook("HA5LA", "GARBAGE", {})
        assert lb.dist(FAR) == 0
        assert lb.bearing(FAR) == 0

    @pytest.mark.parametrize("bad", ["", "ZZ99"])
    def test_bridge_loc_distance_str_is_empty(self, bad):
        assert bridge._loc_distance_str(HOME, bad) == ""
        assert bridge._loc_distance_str(bad, HOME) == ""

    def test_bridge_loc_distance_str_when_valid(self):
        assert bridge._loc_distance_str(HOME, FAR).startswith(" | ")

    def test_sked_text_omits_distance_on_bad_locator(self):
        msg = bridge.sked_text("HA7NS", "HA5LA", HOME, "ZZ99")
        assert "km" not in msg
        assert msg.startswith("Hi HA7NS")

    def test_sked_text_includes_distance_when_valid(self):
        assert "km" in bridge.sked_text("HA7NS", "HA5LA", HOME, FAR)
