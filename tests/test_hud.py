"""Tests for the HUD's data layer: what the bar shows at any moment.

Nothing here draws; hud_draw's own tests cover that half."""

from datetime import datetime, timezone

from urhpk import hud, rig_state, video_format
from urhpk import timeline as tl
from urhpk.cw_decode import (
    CharEvent,
)
from urhpk.geo import initial_bearing, maidenhead_to_latlon
from urhpk.rig_state import (
    SegState,
    TelemetrySample,
)
from urhpk.scope_render import (
    SCOPE_AMP_MAX,
)
from urhpk.timeline import (
    Qso,
    Segment,
)


def _hud_seg(dur=600.0, audio_t=0.0, wall=None):
    return Segment("a", wall or datetime(2026, 8, 3, 20, 0, 0), dur, audio_t)


HOME = "JN97TF"


def _hud_qso(callsign, pts, loc="JN97TF", dup=False):
    return Qso(
        datetime(2026, 8, 3, 18, 0), callsign, "599", "001", "599", "001", loc, pts, dup
    )


class TestHudGeo:
    def test_rejects_anything_that_is_not_a_locator(self):
        assert maidenhead_to_latlon("") is None
        assert maidenhead_to_latlon("ZZ99") is None  # field letters stop at R
        assert maidenhead_to_latlon("JN9") is None
        assert maidenhead_to_latlon("JN97TF") is not None

    def test_six_character_locator_sits_inside_its_own_four_character_square(self):
        lat4, lon4 = maidenhead_to_latlon("JN97")
        lat6, lon6 = maidenhead_to_latlon("JN97TF")
        assert abs(lat6 - lat4) < 0.5
        assert abs(lon6 - lon4) < 1.0

    def test_initial_bearing_matches_the_cardinal_directions(self):
        assert abs(initial_bearing(0, 0, 10, 0) - 0) < 0.1  # due north
        assert abs(initial_bearing(0, 0, 0, 10) - 90) < 0.1  # due east
        assert abs(initial_bearing(0, 0, -10, 0) - 180) < 0.1  # due south


class TestHudQsoMarks:
    def test_accumulates_score_count_and_best_dx_at_each_qso_end(self):
        # The scores are deliberately not the distances: ODX is captioned in km
        # and is measured, while a QSO's points are its kilometres rounded up.
        qsos = [
            _hud_qso("HA1A", 38, loc="JN97WM"),  # 37 km
            _hud_qso("HA2B", 233, loc="JN87GF"),  # 232 km
            _hud_qso("HA3C", 0, loc="IO83RO", dup=True),  # 1713 km, uncounted
        ]
        windows = [(0.0, 10.0), (20.0, 30.0), (40.0, 50.0)]
        assert hud.hud_qso_marks(qsos, windows, HOME) == [
            (10.0, 38, 1, 37),
            (30.0, 271, 2, 232),
            (50.0, 271, 3, 232),  # a dup adds a QSO but no score and no best DX
        ]

    def test_a_qso_whose_locator_will_not_parse_is_no_ones_best_dx(self):
        qsos = [_hud_qso("HA1A", 38, loc="JN97WM"), _hud_qso("HA2B", 0, loc="")]
        windows = [(0.0, 10.0), (20.0, 30.0)]
        assert [m[3] for m in hud.hud_qso_marks(qsos, windows, HOME)] == [37, 37]

    def test_marks_are_ordered_by_window_end_not_by_edi_order(self):
        # qso_windows can hand back a QSO whose exact submit time reorders it
        # relative to the EDI's minute-precision sort.
        qsos = [_hud_qso("HA1A", 100), _hud_qso("HA2B", 300)]
        windows = [(30.0, 40.0), (0.0, 10.0)]
        assert [m[0] for m in hud.hud_qso_marks(qsos, windows, HOME)] == [10.0, 40.0]


class TestHudTimeline:
    def test_score_counts_up_over_the_animation_window_then_holds(self):
        tl = hud.HudTimeline(
            segs=[_hud_seg()], qso_marks=[(10.0, 100, 1, 100), (20.0, 400, 2, 300)]
        )
        assert tl.at(9.9).score == 0
        assert tl.at(10.0).score == 0  # the count-up starts from the old total
        midway = tl.at(10.0 + hud.HUD_SCORE_ANIM_S / 2).score
        assert 0 < midway < 100
        assert tl.at(10.0 + hud.HUD_SCORE_ANIM_S).score == 100
        assert tl.at(19.0).score == 100  # holds until the next QSO
        assert tl.at(20.0 + hud.HUD_SCORE_ANIM_S).score == 400

    def test_score_flash_decays_to_zero_over_the_same_window(self):
        tl = hud.HudTimeline(segs=[_hud_seg()], qso_marks=[(10.0, 100, 1, 100)])
        assert tl.at(10.0).score_flash == 1.0
        assert tl.at(10.0 + hud.HUD_SCORE_ANIM_S).score_flash < 1e-6
        assert tl.at(30.0).score_flash == 0.0

    def test_rate_counts_only_qsos_inside_the_trailing_window(self):
        window = hud.HUD_RATE_WINDOW_S
        tl = hud.HudTimeline(
            segs=[_hud_seg(dur=window * 2)],
            qso_marks=[(0.0, 1, 1, 1), (100.0, 2, 2, 1), (window + 50.0, 3, 3, 1)],
        )
        # At window+60 the first QSO has aged out; two remain inside.
        assert tl.at(window + 60.0).rate_per_h == 2 * 3600.0 / window

    def test_target_bearing_only_shows_inside_its_own_qso_window(self):
        tl = hud.HudTimeline(segs=[_hud_seg()], target_spans=[(10.0, 20.0, 271.0)])
        assert tl.at(9.0).target_az is None
        assert tl.at(15.0).target_az == 271.0
        assert tl.at(20.0).target_az is None

    def test_rig_state_supplies_band_from_the_frequency(self):
        events = [(0.0, 10.0, SegState(ptt=True, freq_hz=432_200_000, mode="CW"))]
        tl = hud.HudTimeline(segs=[_hud_seg()], state_events=events)
        state = tl.at(5.0)
        assert (state.ptt, state.mode, state.band) == (True, "CW", "70CM")
        assert tl.at(15.0).band is None  # past the run, nothing carries over

    def test_a_chip_lights_over_the_rise_time_when_its_band_is_selected(self):
        tl = hud.HudTimeline(segs=[_hud_seg()], chip_marks=[(0.0, "2M", "SSB")])
        assert tl.at(0.0).chip_glow.get("2M", 0.0) == 0.0
        assert 0.0 < tl.at(hud.HUD_CHIP_RISE_S / 2).chip_glow["2M"] < 1.0
        assert tl.at(hud.HUD_CHIP_RISE_S).chip_glow["2M"] == 1.0

    def test_a_chip_glows_down_more_slowly_than_it_lights(self):
        marks = [(0.0, "2M", "SSB"), (10.0, "70CM", "SSB")]
        tl = hud.HudTimeline(segs=[_hud_seg()], chip_marks=marks)
        assert tl.at(10.0).chip_glow["2M"] == 1.0
        # The one arriving is fully lit while the one leaving is still glowing.
        assert tl.at(10.0 + hud.HUD_CHIP_RISE_S).chip_glow["70CM"] == 1.0
        assert tl.at(10.0 + hud.HUD_CHIP_RISE_S).chip_glow["2M"] > 0.0
        assert tl.at(10.0 + hud.HUD_CHIP_DECAY_S).chip_glow["2M"] < 1e-6

    def test_a_chip_switched_back_on_mid_fade_resumes_from_where_it_got_to(self):
        half = hud.HUD_CHIP_DECAY_S / 2
        marks = [(0.0, "2M", "SSB"), (10.0, "70CM", "SSB"), (10.0 + half, "2M", "SSB")]
        tl = hud.HudTimeline(segs=[_hud_seg()], chip_marks=marks)
        assert abs(tl.at(10.0 + half).chip_glow["2M"] - 0.5) < 1e-6
        # Half-faded, so it needs only half the rise time back to full.
        assert tl.at(10.0 + half + hud.HUD_CHIP_RISE_S / 2).chip_glow["2M"] > 1 - 1e-6

    def test_the_mode_chips_are_unaffected_by_a_band_change(self):
        marks = [(0.0, "2M", "SSB"), (10.0, "70CM", "SSB")]
        tl = hud.HudTimeline(segs=[_hud_seg()], chip_marks=marks)
        assert tl.at(10.0 + hud.HUD_CHIP_DECAY_S).chip_glow["SSB"] == 1.0

    def test_signal_level_clears_when_the_scope_recording_stops(self):
        tl = hud.HudTimeline(segs=[_hud_seg()], s_marks=[(5.0, 0.5)])
        assert tl.at(5.0).s_level == 0.5
        assert tl.at(5.0 + hud.HUD_S_HOLD_S).s_level == 0.5
        assert tl.at(5.0 + hud.HUD_S_HOLD_S + 0.1).s_level is None

    def test_utc_is_the_local_wall_clock_less_the_derived_offset(self):
        tl = hud.HudTimeline(segs=[_hud_seg()], offset_h=2)
        assert tl.at(30.0).utc == datetime(2026, 8, 3, 18, 0, 30)


class TestHudCompass:
    def _tl(self, marks):
        return hud.HudTimeline(segs=[_hud_seg()], az_marks=marks)

    def test_the_needle_sweeps_between_samples_instead_of_stepping(self):
        # The real bug, from the August round: the operator turned 250 -> 31
        # degrees over half a minute and the needle stood still, then jumped
        # at the end, because the bearing came from a per-run median and the
        # whole slew sat inside one run.
        tl = self._tl([(10.0, 250.0), (11.0, 256.0)])
        assert tl.at(10.0).rot_az == 250.0
        assert tl.at(10.5).rot_az == 253.0
        assert tl.at(11.0).rot_az == 256.0

    def test_the_sweep_takes_the_short_way_round_north(self):
        # 358 -> 3 is five degrees clockwise, not 355 the other way.
        tl = self._tl([(10.0, 358.0), (11.0, 3.0)])
        assert tl.at(10.4).rot_az == 0.0  # 358 + 2 == 360 == due north
        assert tl.at(10.6).rot_az == 1.0

    def test_crossing_north_stays_smooth_frame_to_frame(self):
        # The wrap is where a bearing interpolation goes wrong if it goes
        # wrong at all -- 359 -> 0 is the one step where the numbers fall
        # rather than rise, and a naive lerp sweeps 355 degrees backwards
        # across the whole card in a single frame. Sampled at the real frame
        # rate over a slew that crosses north, every step must be small and
        # forward. Checked against the August round's own 352 -> 12 slew,
        # where the drawn needle (measured back off its pixels) tracks the
        # interpolated bearing to within a few tenths of a degree.
        tl = self._tl([(10.0, 352.0), (11.0, 358.0), (12.0, 4.0)])
        angles = [tl.at(10.0 + i / video_format.RENDER_FPS).rot_az for i in range(61)]
        steps = [(b - a + 180) % 360 - 180 for a, b in zip(angles, angles[1:])]
        assert all(0 <= s <= 1 for s in steps), max(steps)

    def test_a_stationary_rotator_holds_rather_than_drifting(self):
        # Change-only telemetry writes nothing while the rotator sits still,
        # so a long gap between samples is not a slow movement -- interpolating
        # across it would creep the needle for minutes through a period when
        # the rotator did not move at all, then it would move instantly.
        tl = self._tl([(10.0, 90.0), (10.0 + hud.HUD_AZ_INTERP_S + 1, 270.0)])
        assert tl.at(10.5).rot_az == 90.0
        assert tl.at(11.9).rot_az == 90.0

    def test_the_last_bearing_holds_to_the_end_of_the_video(self):
        tl = self._tl([(10.0, 90.0)])
        assert tl.at(9.9).rot_az is None  # nothing known yet
        assert tl.at(500.0).rot_az == 90.0

    def test_an_explicit_offline_mark_ends_the_needle(self):
        # The rotator going offline is itself an event: the logger writes one
        # {"az": null} line at the transition and then stays quiet. Treating
        # that as "this record just doesn't mention az" would sail past the
        # rotator dying and point the needle at its last bearing for the rest
        # of the video.
        tl = self._tl([(10.0, 90.0), (20.0, None)])
        assert tl.at(15.0).rot_az == 90.0
        assert tl.at(25.0).rot_az is None


class TestHudSources:
    def test_az_marks_read_every_rotator_event_and_nothing_else(self):
        # The mirror image of the offline mark above: a rig event carries no
        # "az" key at all, which also loads as az=None but means only that the
        # line is silent about the rotator. Filtering on `az is not None`
        # alone would confuse the two in one direction or the other.
        segs = [_hud_seg()]  # 20:00 local == 18:00 UTC at offset 2
        telemetry = [
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 5), None, None, 135.0),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 7), 144174000, "CW", None),
            TelemetrySample(
                datetime(2026, 8, 3, 18, 0, 9), None, None, None, az_offline=True
            ),
        ]
        assert hud.hud_az_marks(telemetry, segs, offset_h=2) == [
            (5.0, 135.0),
            (9.0, None),
        ]

    def test_chip_marks_land_only_where_the_band_or_mode_changes(self):
        segs = [_hud_seg()]  # 20:00 local == 18:00 UTC at offset 2
        telemetry = [
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 1), 144174000, "CW", None),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 2), 144174000, "CW", None),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 3), None, None, 135.0),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 4), 144300000, "SSB", None),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 5), 432200000, "SSB", None),
        ]
        assert hud.hud_chip_marks(telemetry, segs, offset_h=2) == [
            (1.0, "2M", "CW"),
            (4.0, "2M", "SSB"),
            (5.0, "70CM", "SSB"),
        ]

    def test_chip_marks_go_dark_when_the_radio_drops_and_relight_after(self):
        segs = [_hud_seg()]
        telemetry = [
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 1), 144174000, "CW", None),
            TelemetrySample(
                datetime(2026, 8, 3, 18, 0, 2), None, None, None, rig_offline=True
            ),
            TelemetrySample(datetime(2026, 8, 3, 18, 0, 3), 144174000, "CW", None),
        ]
        assert hud.hud_chip_marks(telemetry, segs, offset_h=2) == [
            (1.0, "2M", "CW"),
            (2.0, None, None),
            (3.0, "2M", "CW"),
        ]

    def test_s_marks_read_the_scope_sweeps_own_centre_bins(self):
        segs = [_hud_seg()]  # 20:00 local == 18:00 UTC at offset 2
        ts = datetime(2026, 8, 3, 18, 0, 30, tzinfo=timezone.utc).timestamp()
        quiet = bytes([10] * 475)
        loud = bytearray([10] * 475)
        loud[475 // 2] = SCOPE_AMP_MAX
        marks = hud.hud_s_marks(
            [(ts, 0, 0, quiet), (ts + 1, 0, 0, bytes(loud))], segs, offset_h=2
        )
        assert marks[0] == (30.0, 10 / SCOPE_AMP_MAX)
        assert marks[1] == (31.0, 1.0)

    def test_s_marks_take_the_loudest_centre_bin_not_the_average(self):
        # A signal sitting in one bin must not be diluted by the quiet bins
        # either side of it.
        segs = [_hud_seg()]
        ts = datetime(2026, 8, 3, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        pixels = bytearray([0] * 475)
        pixels[475 // 2] = 80
        marks = hud.hud_s_marks([(ts, 0, 0, bytes(pixels))], segs, offset_h=2)
        assert marks[0][1] == 80 / SCOPE_AMP_MAX

    def test_target_spans_give_the_bearing_to_each_worked_station(self):
        # JN87 sits west-north-west of JN97TF (2.6 degrees of longitude
        # west, a quarter degree north), i.e. a bearing just short of 280.
        spans = hud.hud_target_spans(
            [_hud_qso("HA1A", 100, loc="JN87")], [(0.0, 10.0)], "JN97TF"
        )
        assert len(spans) == 1
        start, end, az = spans[0]
        assert (start, end) == (0.0, 10.0)
        assert 275 < az < 285

    def test_target_spans_skip_a_qso_whose_locator_will_not_parse(self):
        spans = hud.hud_target_spans(
            [_hud_qso("HA1A", 100, loc="?????")], [(0.0, 10.0)], "JN97TF"
        )
        assert spans == []

    def test_wall_time_at_inverts_audio_time_for(self):
        segs = [
            Segment("a", datetime(2026, 8, 3, 20, 0, 0), 60.0, 0.0),
            Segment("b", datetime(2026, 8, 3, 20, 1, 0), 60.0, 60.0),
        ]
        for t in (0.0, 30.0, 59.0, 60.0, 90.0):
            assert tl.audio_time_for(hud.wall_time_at(t, segs), segs) == t


class TestMeterCalibration:
    def test_vd_matches_the_multimeter_reading_it_was_checked_against(self):
        # Raw 152 was measured on the real radio while a multimeter read
        # 13.78 V -- Icom's own Vd curve lands within 1%.
        assert abs(hud.vd_volts(152) - 13.78) < 0.15

    def test_po_and_swr_hit_their_published_calibration_points(self):
        assert hud.po_percent(213) == 100.0
        assert hud.po_percent(143) == 50.0
        assert hud.swr_ratio(0) == 1.0
        assert hud.swr_ratio(48) == 1.5
        assert hud.swr_ratio(120) == 3.0

    def test_id_uses_the_measured_line_not_icoms_curve(self):
        # Icom's IC-7300 curve gives 17.6 A for raw 171. Measured against a
        # multimeter in series, PA drain fits a line through the origin at
        # 0.0741 A/raw -- ~12.7 A there, and ~17.9 A full scale, not 25 A.
        assert hud.id_amps(0) == 0.0
        assert abs(hud.id_amps(171) - 12.67) < 0.1
        # The low-current cluster the line was fitted through. The bound is
        # 6% because the lowest point sits 5.3% off: a 20 A meter range
        # resolves ~5 A poorly, and the constant-receive-baseline assumption
        # is least safe there.
        for raw, amps in ((55, 3.87), (61, 4.48), (64, 4.71)):
            assert abs(hud.id_amps(raw) - amps) / amps < 0.06

    def test_id_stays_linear_through_zero(self):
        # Two points a factor of three apart in current agreed to 1% on the
        # same through-origin slope, so a curve that bends is a regression.
        assert abs(hud.id_amps(120) - 2 * hud.id_amps(60)) < 0.01

    def test_a_missing_reading_stays_missing_rather_than_becoming_zero(self):
        # An old recording has no meter data at all; the PWR panel must show
        # its placeholder rather than a confident 0.0 V.
        assert hud.vd_volts(None) is None
        assert hud.id_amps(None) is None

    def test_meters_reach_the_hud_state(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(
            '{"t": "2026-08-03T18:00:30.000000Z", "vd": 152, "id": 171,'
            ' "swr": 28, "po": 213}\n'
        )
        telemetry = rig_state.load_telemetry(str(f))
        assert (telemetry[0].vd, telemetry[0].id_raw) == (152, 171)
        tl = hud.HudTimeline(
            segs=[_hud_seg()],
            offset_h=2,
            meter_marks=hud.hud_meter_marks(telemetry, [_hud_seg()], 2),
        )
        assert tl.at(20.0).vd is None  # before the first reading
        assert abs(tl.at(60.0).vd - 13.78) < 0.15
        assert abs(tl.at(60.0).id_a - 12.67) < 0.1

    def test_a_radio_disconnect_clears_the_meters_rather_than_holding_them(
        self, tmp_path
    ):
        # A real session dropped three times in nine minutes. Meters are
        # change-only and a supply voltage has no reason to change, so without
        # an explicit null the pre-outage reading would be shown for the whole
        # outage.
        f = tmp_path / "t.jsonl"
        f.write_text(
            '{"t": "2026-08-03T18:00:30.000000Z", "vd": 152, "id": 171,'
            ' "swr": 28, "po": 213}\n'
            '{"t": "2026-08-03T18:01:00.000000Z", "vd": null, "id": null,'
            ' "swr": null, "po": null}\n'
        )
        telemetry = rig_state.load_telemetry(str(f))
        assert telemetry[1].meters_offline
        segs = [_hud_seg()]
        tl = hud.HudTimeline(
            segs=segs, offset_h=2, meter_marks=hud.hud_meter_marks(telemetry, segs, 2)
        )
        assert tl.at(45.0).vd is not None  # while the radio was there
        assert tl.at(120.0).vd is None  # and gone once it dropped
        assert tl.at(120.0).id_a is None


def _cw_segs():
    return [
        Segment(
            "a", datetime(2026, 7, 4, 13, 0, 0), 5.0, 0.0,
            events=[CharEvent(0.5, "H"), CharEvent(0.6, "I")],
        )
    ]  # fmt: skip


def _ticker_at(t, segs, cw_spans=None):
    tl = hud.HudTimeline(
        segs=segs,
        stream=hud.ticker_stream(hud.ticker_chunks(segs, cw_spans)),
    )
    return "".join(ch for _, ch in tl.at(t).ticker)


class TestTickerScrolling:
    def test_characters_march_off_the_display_on_their_own(self):
        # The whole point of scrolling on a clock: no clearing rule, no flush,
        # no staleness horizon. A character keyed at t=0.5 has physically left
        # a HUD_TICKER_SPAN_S-wide display well before t=30, so the leak bugs
        # the old static transcript needed guarding against cannot occur.
        segs = _cw_segs()
        assert "H" in _ticker_at(1.0, segs)
        assert _ticker_at(hud.HUD_TICKER_SPAN_S + 2.0, segs) == ""

    def test_a_later_burst_never_shares_the_display_with_an_earlier_one(self):
        segs = [
            Segment(
                "a", datetime(2026, 7, 4, 13, 0, 0), 10.0, 0.0,
                events=[CharEvent(1.0, "A"), CharEvent(2.0, "B")],
            ),
            Segment("b", datetime(2026, 7, 4, 13, 0, 10), 474.0, 10.0),
            Segment(
                "c", datetime(2026, 7, 4, 13, 7, 4), 5.0, 484.0,
                events=[CharEvent(0.01, "X"), CharEvent(0.6, "Y")],
            ),
        ]  # fmt: skip
        shown = _ticker_at(484.5, segs)
        assert "X" in shown and "A" not in shown and "B" not in shown

    def _tl(self, keyed, dur=30.0):
        seg = Segment(
            "a", datetime(2026, 7, 4, 13, 0, 0), dur, 0.0,
            events=[CharEvent(t, c) for t, c in keyed],
        )  # fmt: skip
        return hud.HudTimeline(
            segs=[seg], stream=hud.ticker_stream(hud.ticker_chunks([seg], None))
        )

    def _spacing(self, tl, t):
        offsets = [o for o, _ in tl.at(t).ticker]
        return [b - a for a, b in zip(offsets, offsets[1:])]

    def test_characters_sit_one_cell_apart_however_irregularly_keyed(self):
        # Morse characters take wildly different air time -- a T is one dit, a
        # 0 is nineteen -- so placing them by keying time spaced them raggedly,
        # by fractions of a cell, for no reason a viewer could see. The
        # spacing is the display's own; the timing drives the scroll instead.
        tl = self._tl([(0.1, "T"), (0.35, "0"), (2.0, "A")])
        assert self._spacing(tl, 2.5) == [hud.HUD_TICKER_CELL_COLS] * 2

    def test_a_character_reaches_the_right_hand_cell_as_it_is_keyed(self):
        # Which is what makes the scroll rate follow the keying: each
        # character is a pin at the right edge, and the strip covers exactly
        # one cell between consecutive pins however long that takes.
        tl = self._tl([(0.1, "T"), (0.35, "0"), (2.0, "A")])
        width = hud.HUD_TICKER_CHARS * hud.HUD_TICKER_CELL_COLS
        for t, ch in ((0.1, "T"), (0.35, "0"), (2.0, "A")):
            newest = tl.at(t).ticker[-1]
            assert newest == (width - hud.HUD_TICKER_CELL_COLS, ch)

    def test_a_real_pause_still_takes_its_real_width(self):
        # Constant spacing only holds within an over. Past
        # HUD_TICKER_BURST_S the operator has stopped sending, and that gap
        # keeps its real duration -- which is what drains the display between
        # overs and makes staleness structurally impossible.
        tl = self._tl([(0.1, "A"), (0.1 + hud.HUD_TICKER_BURST_S + 1.0, "B")])
        assert self._spacing(tl, 4.2)[0] > hud.HUD_TICKER_CELL_COLS

    def test_fast_keying_never_overlaps(self):
        tl = self._tl([(i * 0.01, c) for i, c in enumerate("ABCDE")])
        assert self._spacing(tl, 0.2) == [hud.HUD_TICKER_CELL_COLS] * 4
