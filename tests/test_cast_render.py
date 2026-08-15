"""Tests for the terminal PiP: replaying a .cast into frames."""

import json
from datetime import datetime

import numpy as np
import pyte
from PIL import Image, ImageDraw, ImageFont

from urhpk import cast_render
from urhpk.cast_render import (
    CAST_BG,
    _cast_color,
    _CastScreen,
    _CastStream,
    _draw_cast_row,
    parse_cast_header,
)


def _write_cast(path, width, height, ts, events):
    """A minimal asciinema cast v2 file for testing, without needing a
    real `asciinema rec` session. `events` is [(t, text)] output events."""
    with open(path, "w") as f:
        f.write(
            json.dumps(
                {"version": 2, "width": width, "height": height, "timestamp": ts}
            )
            + "\n"
        )
        for t, text in events:
            f.write(json.dumps([t, "o", text]) + "\n")


class TestTerminalCast:
    """Rendering an asciinema .cast (e.g. an irssi+logger tmux session) as
    a video PIP -- see render_cast_video's docstring for why this is text
    rasterized via pyte+PIL rather than a GIF/agg conversion, and why sync
    needs no cross-correlation at all (the cast's own header embeds an
    exact Unix-epoch start time, from the same machine's clock)."""

    def test_parse_cast_header_reads_exact_utc_start(self, tmp_path):
        p = tmp_path / "session.cast"
        # 1783890785 is 2026-07-12 21:13:05 UTC (real value from a real
        # asciinema recording, cross-checked against `date -d @1783890785`)
        _write_cast(str(p), 191, 52, 1783890785, [])
        start, w, h = parse_cast_header(str(p))
        assert start == datetime(2026, 7, 12, 21, 13, 5)
        assert (w, h) == (191, 52)

    def test_cast_color_falls_back_to_default_for_unknown_name(self):
        default = (1, 2, 3)
        assert _cast_color(None, default) == default
        assert _cast_color("default", default) == default
        assert _cast_color("nonexistent-color-name", default) == default

    def test_cast_color_looks_up_known_names(self):
        assert _cast_color("red", (0, 0, 0)) == cast_render.CAST_PALETTE["red"]

    def test_draw_cast_row_descender_survives_the_row_belows_own_redraw(self):
        # Regression test for a real bug: a naive 1.2x-of-font-size line
        # height (15px at CAST_FONT_SIZE=13) undershot DejaVu Sans Mono's
        # real metrics (ascent+descent=17px). _draw_cast_row erases and
        # redraws exactly one row's own rectangle at a time (see its
        # docstring), so a descender glyph like '_' that spilled past a
        # too-short row height got clipped the next time the row *below*
        # was independently redrawn -- even though the row with the
        # underscore never changed. Found from a real rendered frame: a
        # static irssi banner's underscores were visibly missing partway
        # through a render.  Verified red before green: this exact
        # comparison showed 39 differing pixels with the old int(size*1.2)
        # formula and 0 with font.getmetrics()-based line height.
        font = ImageFont.truetype(
            cast_render.CAST_FONT_PATH, cast_render.CAST_FONT_SIZE
        )
        font_b = ImageFont.truetype(
            cast_render.CAST_FONT_BOLD, cast_render.CAST_FONT_SIZE
        )
        cw = font.getlength("M")
        ascent, descent = font.getmetrics()
        lh = ascent + descent
        W, H = 5, 2

        screen = pyte.Screen(W, H)
        stream = pyte.ByteStream(screen)
        stream.feed(b"_____")

        px_w, px_h = int(cw * W) + 4, lh * H + 4
        crop_h = min(px_h, lh + 5)  # a margin below row 0's own rectangle

        canvas_alone = Image.new("RGB", (px_w, px_h), CAST_BG)
        _draw_cast_row(
            ImageDraw.Draw(canvas_alone), screen.buffer[0], 0, W, font, font_b, cw, lh
        )
        row0_alone = np.array(canvas_alone.crop((0, 0, px_w, crop_h)))

        canvas_after = Image.new("RGB", (px_w, px_h), CAST_BG)
        draw_after = ImageDraw.Draw(canvas_after)
        _draw_cast_row(draw_after, screen.buffer[0], 0, W, font, font_b, cw, lh)
        _draw_cast_row(draw_after, screen.buffer[1], 1, W, font, font_b, cw, lh)
        row0_after_row1_redraw = np.array(canvas_after.crop((0, 0, px_w, crop_h)))

        assert np.array_equal(row0_alone, row0_after_row1_redraw)

    # tmux (the logger is recorded running inside it) scrolls/clears a single
    # pane with left/right margins (DECSLRM, CSI Pl;Pr s) + scroll-up (SU,
    # CSI Ps S) -- three sequences stock pyte drops, so a pane never cleared
    # and old content showed through the new (the reported "startup screen
    # still visible behind the contest screen" garbage). _CastScreen/
    # _CastStream implement them; `asciinema play` always did, which is why
    # the cast looked clean there but not in the render.
    _CLEAR = b"\x1b[1;4r\x1b[11;20s\x1b[4S\x1b[1;4r"  # tmux clears cols 11-20

    def test_stock_pyte_leaves_stale_pane_content(self):
        # The bug this fixes: with plain pyte the pane-scroll is a no-op, so
        # the old tail survives -- exactly the garbage seen in the render.
        screen = pyte.Screen(20, 4)
        stream = pyte.ByteStream(screen)
        stream.feed(b"\x1b[1;1H0123456789ABCDEFGHIJ")
        stream.feed(self._CLEAR)
        assert screen.display[0][10:] == "ABCDEFGHIJ"  # NOT cleared

    def test_cast_screen_clears_only_the_pane_columns(self):
        screen = _CastScreen(20, 4)
        stream = _CastStream(screen)
        stream.feed(b"\x1b[1;1H0123456789ABCDEFGHIJ")
        assert screen.display[0] == "0123456789ABCDEFGHIJ"
        stream.feed(self._CLEAR)
        assert screen.display[0][:10] == "0123456789"  # left pane untouched
        assert screen.display[0][10:] == " " * 10  # right pane cleared

    def test_bare_csi_s_is_save_cursor_not_a_margin(self):
        # `CSI s` with <2 params is SCOSC (save cursor), not DECSLRM -- must
        # not set a left/right margin (which would wrongly constrain scrolls).
        screen = _CastScreen(20, 4)
        _CastStream(screen).feed(b"\x1b[s")
        assert screen.margins_lr is None

    def _build_two_pane_screen(self, cls):
        # 20 cols x 4 rows: row0 is a full-width header (must stay outside
        # the vertical scroll margins); rows1-3 hold distinct left-pane
        # (cols0-9) and right-pane (cols10-19) content, like irssi | logger.
        screen = cls(20, 4)
        stream = pyte.ByteStream(screen) if cls is pyte.Screen else _CastStream(screen)
        stream.feed(b"\x1b[1;1H" + b"H" * 20)
        stream.feed(b"\x1b[2;1H0000000000")
        stream.feed(b"\x1b[3;1H1111111111")
        stream.feed(b"\x1b[4;1H2222222222")
        stream.feed(b"\x1b[2;11HAAAAAAAAAA")
        stream.feed(b"\x1b[3;11HBBBBBBBBBB")
        stream.feed(b"\x1b[4;11HCCCCCCCCCC")
        return screen, stream

    def test_stock_pyte_plain_linefeed_drags_the_other_pane_too(self):
        # The bug this fixes: a plain '\n' hitting the bottom margin uses
        # pyte's stock index(), which swaps whole row *objects*
        # (buffer[y] = buffer[y+1]) -- ignoring margins_lr entirely, unlike
        # the explicit-SU path _scroll already handles. Found from the real
        # cast: irssi filling its pane and auto-scrolling on a plain
        # linefeed dragged the logger's pane (outside DECSLRM) up with it.
        screen, stream = self._build_two_pane_screen(pyte.Screen)
        stream.feed(b"\x1b[2;4r\x1b[1;10s")  # DECSTBM rows1-3, DECSLRM cols0-9
        stream.feed(b"\x1b[4;1H\n")  # cursor at bottom margin, plain LF
        # right pane (untouched by DECSLRM) wrongly scrolled too
        assert screen.display[1][10:] == "BBBBBBBBBB"

    def test_cast_screen_plain_linefeed_only_scrolls_its_own_pane(self):
        screen, stream = self._build_two_pane_screen(_CastScreen)
        stream.feed(b"\x1b[2;4r\x1b[1;10s")  # DECSTBM rows1-3, DECSLRM cols0-9
        stream.feed(b"\x1b[4;1H\n")  # cursor at bottom margin, plain LF
        assert screen.display[0] == "H" * 20  # header untouched
        assert screen.display[1] == "1111111111AAAAAAAAAA"  # left scrolled...
        assert screen.display[2] == "2222222222BBBBBBBBBB"  # ...right didn't
        assert screen.display[3] == "          CCCCCCCCCC"
