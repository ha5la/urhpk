"""The QTH map: the camera that frames a path, and the shot it holds."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

from urhpk.qth_map import (
    DX_COLOUR,
    SWING_S,
    camera_at,
    camera_for,
    draw_map_frame,
    map_targets,
    mip_pyramid,
    mosaic,
    project,
    row_taps,
    tile_span_km,
    zoom_for,
)
from urhpk.timeline import Qso

ME = "JN97TF"
W, H = 672, 350


def _qso(callsign: str, loc: str) -> Qso:
    return Qso(
        dt=datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc),
        callsign=callsign,
        rst_s="59",
        nr_s="001",
        rst_r="59",
        nr_r="001",
        loc=loc,
        pts=1,
        dup=False,
    )


class TestTheCamera:
    def test_frames_the_whole_path_whatever_its_length(self):
        """The QTH low, the worked station high, with no per-station tuning --
        this is what lets one rule serve 33 km and 233 km alike."""
        for distance in (33.0, 47.0, 168.0, 233.0):
            cam = camera_for(307.0, distance)
            _, y_me = project(cam, 307.0, 0.0, W, H)
            _, y_dx = project(cam, 307.0, distance, W, H)
            assert 0.75 * H < y_me < H, f"{distance} km: QTH at {y_me}"
            assert 0 < y_dx < 0.25 * H, f"{distance} km: station at {y_dx}"

    def test_keeps_the_qth_at_frame_centre_whichever_way_it_looks(self):
        """The camera sits directly behind the QTH along the bearing, so the
        QTH cannot drift sideways as the view turns."""
        for bearing in (0.0, 90.0, 251.0, 307.0, 359.0):
            x, _ = project(camera_for(bearing, 90.0), bearing, 0.0, W, H)
            assert x == pytest.approx(W / 2)

    def test_a_station_off_to_one_side_lands_off_to_one_side(self):
        cam = camera_for(307.0, 90.0)
        x_left, _ = project(cam, 287.0, 90.0, W, H)
        x_right, _ = project(cam, 327.0, 90.0, W, H)
        assert x_left < W / 2 < x_right

    def test_a_station_behind_the_camera_has_no_screen_position(self):
        assert project(camera_for(307.0, 90.0), 127.0, 90.0, W, H) is None


class TestTheFrame:
    @pytest.mark.parametrize("w, h", [(448, 232), (672, 348)])  # 720p, 1080p
    def test_draws_the_worked_station_below_the_caption(self, w, h):
        """The station being worked is the point of the shot, at either render
        size, and its name must not land on the caption naming it."""

        blank = [mip_pyramid(np.zeros((64, 64, 3), np.uint8), 4.0, (32.0, 32.0))]
        targets = map_targets([_qso("HG5O", "JN97NL")], [(0.0, 60.0)], ME)
        frame = np.asarray(
            draw_map_frame(
                blank,
                camera_at(targets, 30.0),
                w,
                h,
                "HA5LA",
            )
        )
        station_rows = np.nonzero((frame == DX_COLOUR).all(axis=2).any(axis=1))[0]
        caption_rows = np.nonzero((frame[: h // 4] > 200).all(axis=2).any(axis=1))[0]
        assert station_rows.size, "the station is not drawn"
        assert station_rows.min() > caption_rows.max()


class TestTheShot:
    def _targets(self):
        qsos = [_qso("HG5O", "JN97NL"), _qso("HA3KHB", "JN86SR")]
        return map_targets(qsos, [(10.0, 20.0), (100.0, 110.0)], ME)

    def test_reads_the_worked_stations_off_the_log(self):
        first, second = self._targets()
        assert (first.callsign, round(first.bearing), round(first.distance_km)) == (
            "HG5O",
            307,
            47,
        )
        assert second.callsign == "HA3KHB"

    def test_a_station_whose_locator_will_not_parse_is_left_out(self):
        targets = map_targets(
            [_qso("HG5O", "JN97NL"), _qso("NOLOC", "")],
            [(10.0, 20.0), (30.0, 40.0)],
            ME,
        )
        assert [t.callsign for t in targets] == ["HG5O"]

    def test_holds_on_the_station_being_worked(self):
        targets = self._targets()
        shot = camera_at(targets, 60.0)
        assert shot.current.callsign == "HG5O"
        assert shot.blend == 1.0

    def test_swings_into_the_next_station_over_the_swing_time(self):
        targets = self._targets()
        start = camera_at(targets, 100.0 - SWING_S)
        middle = camera_at(targets, 100.0 - SWING_S / 2)
        end = camera_at(targets, 100.0)
        assert (start.blend, end.blend) == (0.0, 1.0)
        assert 0.0 < middle.blend < 1.0
        assert start.previous.callsign == "HG5O"
        assert end.current.callsign == "HA3KHB"

    def test_the_swing_takes_the_bearing_the_short_way_round(self):
        """350 deg to 010 deg is twenty degrees east, not three hundred and
        forty west."""
        qsos = [_qso("A", "JN97NL"), _qso("B", "JN86SR")]
        targets = [
            replace(t, bearing=b)
            for t, b in zip(
                map_targets(qsos, [(0.0, 5.0), (100.0, 105.0)], ME), (350.0, 10.0)
            )
        ]
        mid = camera_at(targets, 100.0 - SWING_S / 2).camera.bearing % 360
        assert mid > 345.0 or mid < 15.0

    def test_holds_the_first_station_before_the_round_reaches_it(self):
        assert camera_at(self._targets(), 0.0).current.callsign == "HG5O"

    def test_a_round_with_no_locators_at_all_has_no_shot(self):
        assert camera_at([], 10.0) is None


class TestTheGround:
    def test_picks_the_coarsest_zoom_that_fills_the_budget(self):
        z = zoom_for(47.44, half_km=90.0, budget_px=2048)
        assert tile_span_km(47.44, z) * 2048 / 256 >= 180.0
        assert tile_span_km(47.44, z + 1) * 2048 / 256 < 180.0

    def test_builds_a_mosaic_from_the_tiles_it_asked_for(self):
        asked = []

        def fake(z, x, y):
            asked.append((z, x, y))
            return np.full((256, 256, 3), (z + x + y) % 256, np.uint8)

        tex, kmpp, origin = mosaic(47.44, 19.21, half_km=30.0, z=9, fetch=fake)
        assert len({z for z, _, _ in asked}) == 1
        assert tex.shape[0] % 256 == tex.shape[1] % 256 == 0
        assert tex.shape[0] * tex.shape[1] // 256**2 == len(asked)
        # the QTH lands inside the mosaic, with the requested margin round it
        assert 30.0 / kmpp <= origin[0] <= tex.shape[1] - 30.0 / kmpp
        assert 30.0 / kmpp <= origin[1] <= tex.shape[0] - 30.0 / kmpp


class TestTheGroundFilter:
    def test_spends_more_taps_where_the_footprint_is_more_stretched(self):
        cam = camera_for(307.0, 47.0)
        lod, taps = row_taps(cam, kmpp=0.415, levels=8, w=W, h=H, maxtaps=16)
        assert taps.max() > taps.min(), "a flat tap count is not anisotropic"
        assert taps.max() <= 16

    def test_raises_the_level_rather_than_aliasing_when_taps_run_out(self):
        """With too few taps to cover the long axis, the answer is a coarser
        level -- undersampling it instead is the shimmer this exists to stop."""
        cam = camera_for(307.0, 47.0)
        _, many = row_taps(cam, 0.415, 8, W, H, maxtaps=16)
        lod_few, _ = row_taps(cam, 0.415, 8, W, H, maxtaps=1)
        lod_many, _ = row_taps(cam, 0.415, 8, W, H, maxtaps=16)
        assert many.max() > 1
        assert (lod_few >= lod_many).all()
        assert (lod_few > lod_many).any()
