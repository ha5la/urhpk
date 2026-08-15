"""Tests for the HUD's drawing layer: the artwork, the sprites and the readouts."""

import json
import math

import numpy as np
from PIL import Image, ImageDraw

from urhpk import cw_decode, hud, hud_draw, video_format

_ART = None


def _art():
    """The repo's own theme, prepared once -- loading the artwork and cutting
    its sprites for every test would dominate the suite's runtime."""
    global _ART
    if _ART is None:
        _ART = hud_draw.hud_art(hud_draw.load_hud_theme())
    return _ART


def _drawn(**over):
    state = hud_draw.hud_demo_state()
    for k, v in over.items():
        setattr(state, k, v)
    return np.asarray(hud_draw.draw_hud_frame(state, _art())).astype(int)


def _magenta(rgb):
    """The sprite sheet's key colour, as _key_magenta itself measures it."""
    rgb = np.asarray(rgb).astype(int)
    return np.minimum(rgb[:, :, 0], rgb[:, :, 2]) - rgb[:, :, 1] > 60


class TestHudTheme_Geometry:
    def test_the_bar_is_drawn_at_the_artworks_own_aspect(self):
        # Uniform scaling or none: squashing the artwork to another aspect
        # turns its compass into an ellipse, which is the specific reason this
        # artwork was chosen. Every supported resolution has to land on the
        # artwork's ratio, not just the 1080p reference.
        _, _, bw, bh = hud_draw.load_hud_theme()["bar"]
        art_aspect = bw / bh
        assert abs(hud_draw.HUD_W / hud_draw.HUD_H - art_aspect) / art_aspect < 0.01
        for W, H in video_format.RESOLUTIONS.values():
            assert abs(W / hud_draw.hud_height(H) - art_aspect) / art_aspect < 0.01

    def test_every_rect_stays_inside_the_bar_and_they_never_overlap(self):
        # theme.json is hand-edited data, so this checks the data, not code.
        theme = hud_draw.load_hud_theme()
        bx, by, bw, bh = theme["bar"]
        rects = [(n, r) for n, _, r in hud_draw.hud_theme_rects(theme)
                 if not n.startswith("sprites.")]  # fmt: skip
        for name, (x, y, w, h) in rects:
            assert bx <= x and by <= y, name
            assert x + w <= bx + bw and y + h <= by + bh, name
        for i, (an, (ax, ay, aw, ah)) in enumerate(rects):
            for bn, (b_x, b_y, b_w, b_h) in rects[i + 1 :]:
                apart = (
                    ax + aw <= b_x
                    or b_x + b_w <= ax
                    or ay + ah <= b_y
                    or b_y + b_h <= ay
                )
                assert apart, f"{an} overlaps {bn}"

    def test_rects_are_scaled_from_artwork_pixels_into_the_bar(self, tmp_path):
        # A slot is positioned relative to the bar's own origin, not the
        # sheet's: the sprite sheet below the bar is not part of what gets
        # drawn, so the bar's top-left is the origin everything is measured
        # from.
        art = tmp_path / "artwork.png"
        Image.new("RGB", (200, 200), (20, 20, 20)).save(art)
        (tmp_path / "theme.json").write_text(
            json.dumps(
                {
                    "bar": [10, 20, 100, 50],
                    "slots": {
                        "score": [30, 30, 20, 10],
                        "lamp": [50, 30, 8, 12],
                        "smeter": [50, 45, 20, 6],
                        "compass": [80, 30, 20, 20],
                    },
                    "chips": {"band": [[60, 30, 10, 10]]},
                    "stats": [[80, 30, 10, 10]],
                    "sprites": {
                        "rx": {"box": [0, 100, 8, 8]},
                        "tx": {"box": [10, 100, 8, 8]},
                        "meter": {"box": [20, 100, 20, 5]},
                        "needle": {"box": [50, 100, 6, 20], "pivot": [53, 118]},
                        "target": {"box": [60, 100, 6, 20], "pivot": [63, 118]},
                    },
                }
            )
        )
        a = hud_draw.hud_art(hud_draw.load_hud_theme(str(tmp_path)), 200, 100)
        assert a.bar.size == (200, 100)
        assert a.slots["score"] == (40, 20, 40, 20)  # 2x scale, bar origin off
        assert a.chips["band"] == [(100, 20, 20, 20)]
        assert a.stats == [(140, 20, 20, 20)]

    def test_sprites_are_cut_out_of_the_magenta_sheet(self):
        theme = hud_draw.load_hud_theme()
        for name, sp in theme["sprites"].items():
            x, y, w, h = sp["box"]
            cut = np.asarray(
                hud_draw._key_magenta(theme["image"].crop((x, y, x + w, y + h)))
            )
            alpha = cut[:, :, 3]
            assert set(np.unique(alpha)) <= {0, 255}, name  # a hard key
            assert alpha.mean() > 20, name  # the sprite itself survived
            assert not _magenta(cut[:, :, :3])[alpha > 0].any(), name

    def test_the_meter_sprite_is_cropped_to_the_leds_alone(self):
        # Which is why the meter's lit fraction is simply lit/segments of the
        # sprite's width: there is no frame inside the box to offset past. An
        # earlier crop included the frame around the LEDs, which then got
        # dimmed along with them and left a sliver of it permanently lit at
        # the left. A rectangle of pure LEDs is also the one sprite with no
        # background to key out at all, hence the <= above.
        bright = np.asarray(_art().sprites["meter"])[:, :, :3].max(axis=2) > 120
        assert bright[:, :2].any() and bright[:, -2:].any()

    def test_no_pink_fringe_survives_onto_the_bar(self):
        # The sheet is not flat #FF00FF in practice -- only 145 pixels of the
        # whole artwork are exactly the key colour -- so a key that trusts the
        # colour exactly, or a soft alpha ramp that keeps edge pixels
        # semi-opaque, leaves a magenta halo around every sprite.
        assert not _magenta(_drawn()).any()


class TestHudDrawing:
    def test_frame_has_the_bar_size(self):
        art = hud_draw.hud_art(hud_draw.load_hud_theme(), 1280, 226)
        assert hud_draw.draw_hud_frame(hud_draw.hud_demo_state(), art).size == (
            1280,
            226,
        )

    def test_an_empty_state_renders_placeholders_rather_than_crashing(self):
        # Every recording made before the meter recorder existed looks like
        # this for the PWR panel, and a --duration cut before the first QSO
        # looks like it for the rest.
        img = hud_draw.draw_hud_frame(hud.HudState(), _art())
        assert img.size == (hud_draw.HUD_W, hud_draw.HUD_H)

    def test_no_readout_paints_outside_its_own_recess(self):
        # The artwork carries every label and frame, so anything drawn outside
        # a recess is drawn over artwork it must not touch -- a readout
        # overflowing its panel would show up here as a changed pixel on a
        # baked caption.
        art = _art()
        boxes = (
            list(art.slots.values())
            + [c for row in art.chips.values() for c in row]
            + art.stats
        )
        allowed = np.zeros((hud_draw.HUD_H, hud_draw.HUD_W), bool)
        for x, y, w, h in boxes:
            allowed[y : y + h, x : x + w] = True
        changed = (_drawn() != np.asarray(art.bar).astype(int)).any(axis=2)
        assert not (changed & ~allowed).any()

    def test_the_face_recess_is_left_untouched_for_the_webcam(self):
        art = _art()
        x, y, w, h = art.slots["face"]
        before = np.asarray(art.bar).astype(int)[y : y + h, x : x + w]
        assert (_drawn()[y : y + h, x : x + w] == before).all()

    def test_score_is_fitted_to_its_recess_instead_of_overflowing(self):
        # Regression: a fixed point size overflowed the SCORE panel the moment
        # the score reached five digits, spilling red digits across the gutter
        # into the QSOS panel. _seven_seg's own shrink loop is what keeps it in.
        # The gutter is artwork, not blank, so the assertion is that it comes
        # out of the render untouched rather than that it is dark.
        art = _art()
        sx, _, sw, _ = art.slots["score"]
        cols = slice(sx + sw, art.slots["qsos"][0])
        assert (
            _drawn(score=123456)[:, cols] == np.asarray(art.bar).astype(int)[:, cols]
        ).all()

    def test_the_lamp_shows_rx_on_receive_and_tx_on_transmit(self):
        x, y, w, h = _art().slots["lamp"]

        def lamp(ptt):
            return _drawn(ptt=ptt)[y : y + h, x : x + w].reshape(-1, 3).mean(axis=0)

        assert lamp(False)[1] > lamp(False)[0]  # RX: green ball
        assert lamp(True)[0] > lamp(True)[1]  # TX: red ball
        # With no rig state at all the artwork's empty socket is left alone.
        assert (lamp(None) < lamp(False)).all()

    def test_the_meter_lights_further_with_a_stronger_signal(self):
        x, y, w, h = _art().slots["smeter"]

        def lit(level):
            band = _drawn(s_level=level)[y : y + h, x : x + w]
            return (band.max(axis=2) > 120).sum()

        assert lit(None) == 0
        assert lit(0.2) < lit(0.6) < lit(1.0)

    def test_only_the_selected_band_and_mode_chips_stay_lit(self):
        art = _art()
        img = _drawn(chip_glow={"2M": 1.0, "CW": 1.0})

        def brightness(rect):
            x, y, w, h = rect
            return img[y : y + h, x : x + w].mean()

        for row, names, active in (
            ("band", hud_draw._HUD_BANDS, "2M"),
            ("mode", hud_draw._HUD_MODES, "CW"),
        ):
            lit = [brightness(r) for r, n in zip(art.chips[row], names) if n == active]
            unlit = [
                brightness(r) for r, n in zip(art.chips[row], names) if n != active
            ]
            assert min(lit) > 2 * max(unlit)

    def test_a_chip_part_way_through_its_ramp_is_drawn_part_way_lit(self):
        art = _art()
        x, y, w, h = dict(zip(hud_draw._HUD_BANDS, art.chips["band"]))["2M"]

        def brightness(glow):
            return _drawn(chip_glow=glow)[y : y + h, x : x + w].mean()

        assert brightness({}) < brightness({"2M": 0.5}) < brightness({"2M": 1.0})

    def test_a_needle_turns_about_its_pivot_not_its_bounding_box(self):
        # These needles pivot on the ball at their base, so almost all of a
        # needle lies *ahead* of the compass centre. Turned about the box
        # centre instead, the same needle would swing around the compass with
        # as much of it behind the centre as in front -- which is what this
        # measures, rather than merely that it moved.
        art = _art()
        cx, cy, cw, ch = art.slots["compass"]
        centre = (cx + cw / 2, cy + ch / 2)
        for az in (0, 90, 180, 270):
            img = _drawn(rot_az=az, target_az=None)[cy : cy + ch, cx : cx + cw]
            red = (img[:, :, 0] - np.maximum(img[:, :, 1], img[:, :, 2])) > 100
            ys, xs = np.nonzero(red)
            ys, xs = ys + cy, xs + cx
            assert len(xs), az
            along = (xs - centre[0]) * math.sin(math.radians(az)) - (
                ys - centre[1]
            ) * math.cos(math.radians(az))
            assert along.max() > 3 * max(1.0, -along.min()), az


class TestHudMatrixFont:
    def test_every_character_the_decoder_can_emit_has_a_glyph(self):
        # MORSE is the complete set the CW decoder can ever produce, so the
        # font is finite and fully determined -- nothing can arrive that the
        # ticker has no glyph for.
        assert set(cw_decode.MORSE.values()) <= set(hud_draw._FONT_5X7)

    def test_every_glyph_is_exactly_five_by_seven_bits(self):
        # A mistyped row is a plausible-looking glyph rather than an error, so
        # the shape of the table is checked here and the glyphs themselves were
        # verified by rendering the whole set as a sheet and reading it.
        for ch, bits in hud_draw._FONT_5X7.items():
            rows = bits.split()
            assert len(rows) == hud.HUD_MATRIX_ROWS, ch
            assert all(len(r) == hud.HUD_MATRIX_COLS for r in rows), ch
            assert set("".join(rows)) <= {"0", "1"}, ch

    def test_an_unknown_character_falls_back_to_a_question_mark(self):
        assert hud_draw._matrix_rows("\u00e9") == hud_draw._matrix_rows("?")


class TestMatrixDisplay:
    def _render(self, cells, chars=4):
        img = Image.new("RGB", (chars * 24, 40), (0, 0, 0))
        hud_draw._draw_matrix_text(
            ImageDraw.Draw(img),
            cells,
            (0, 0, chars * 24, 40),
            hud_draw.HUD_GREEN,
            chars,
        )
        return np.asarray(img)[:, :, 1]

    def test_unlit_dots_are_still_drawn_so_an_idle_display_reads_as_one(self):
        green = self._render([])
        assert green.max() > 0  # the dot grid is there
        assert green.max() < hud_draw.HUD_GREEN[1]  # but nothing is lit

    def test_a_lit_glyph_reaches_full_brightness(self):
        assert self._render([(0, "8")]).max() == hud_draw.HUD_GREEN[1]

    def test_a_character_scrolling_off_the_edge_is_clipped_not_wrapped(self):
        # Partly past the left edge: some columns drawn, nothing appearing on
        # the far right.
        green = self._render([(-2, "8")])
        assert green[:, -10:].max() < hud_draw.HUD_GREEN[1]
        assert green.max() == hud_draw.HUD_GREEN[1]


class TestHudTheme:
    def _theme(self, tmp_path, **over):
        art = tmp_path / "artwork.png"
        Image.new("RGB", (200, 100), (20, 20, 20)).save(art)
        theme = {
            "artwork": "artwork.png",
            "bar": [0, 0, 200, 60],
            "slots": {"score": [5, 5, 40, 30]},
            "chips": {"band": [[50, 5, 10, 10], [62, 5, 10, 10]]},
            "stats": [[100, 5, 30, 10]],
            "sprites": {"needle": {"box": [150, 60, 20, 30], "pivot": [160, 85]}},
        }
        theme.update(over)
        (tmp_path / "theme.json").write_text(json.dumps(theme))
        return str(tmp_path)

    def test_load_reads_the_json_and_its_artwork(self, tmp_path):
        theme = hud_draw.load_hud_theme(self._theme(tmp_path))
        assert theme["image"].size == (200, 100)
        assert theme["slots"]["score"] == [5, 5, 40, 30]

    def test_every_positioned_rect_is_flattened_for_the_overlay(self, tmp_path):
        # One score slot, two chips, one stats row, one sprite.
        names = [
            n
            for n, _, _ in hud_draw.hud_theme_rects(
                hud_draw.load_hud_theme(self._theme(tmp_path))
            )
        ]
        assert names == [
            "slots.score", "chips.band[0]", "chips.band[1]",
            "stats[0]", "sprites.needle",
        ]  # fmt: skip

    def test_overlay_marks_every_rect_and_the_needle_pivot(self, tmp_path):
        theme = hud_draw.load_hud_theme(self._theme(tmp_path))
        a = np.asarray(hud_draw.hud_theme_overlay(theme))
        # cyan slot outline, orange chip, yellow stats row, green sprite, red pivot
        for colour in ((0, 255, 255), (255, 140, 0), (255, 255, 0), (0, 255, 0)):
            assert (a == np.array(colour)).all(axis=2).any(), colour
        assert a[:, :, 0].max() == 255

    def test_a_theme_without_sprites_or_chips_still_renders(self, tmp_path):
        # A hand-edited theme mid-edit may be missing whole groups; the check
        # tool has to survive that or it is useless exactly when needed.
        path = self._theme(tmp_path, chips={}, stats=[], sprites={})
        assert hud_draw.hud_theme_overlay(hud_draw.load_hud_theme(path)).size == (
            200,
            100,
        )
