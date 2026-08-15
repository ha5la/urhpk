"""The HUD's drawing layer: turning a HudState into a frame.

The bar *is* the finished artwork (hud-theme/). It carries every panel, recess,
static label and the compass rose, so nothing static is drawn here -- drawing a
label the artwork already bakes would simply print it twice. What this module
draws is only what changes: the readouts, five sprites, and the dimming of
whatever is not currently selected.

theme.json holds every coordinate in *artwork pixels*, hand-verified against
the image (see --hud-theme-check). Coordinates as data rather than as source is
what makes the artwork replaceable: new art means a new theme.json, not a code
change.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from urhpk.hud import (
    HUD_MATRIX_ROWS,
    HUD_TICKER_CELL_COLS,
    HUD_TICKER_CHARS,
    HudState,
    HudTimeline,
)
from urhpk.progress import stage_bar
from urhpk.video_format import RENDER_FPS
from urhpk.wiring import PROJECT_ROOT

# --- the artwork: theme file, sprites, prepared layout -----------------------
#
# The bar *is* the finished artwork (hud-theme/). It carries every panel,
# recess, static label and the compass rose, so nothing static is drawn here --
# drawing a label the artwork already bakes would simply print it twice. What
# this file draws is only what changes: the readouts, five sprites, and the
# dimming of whatever is not currently selected.
#
# theme.json holds every coordinate in *artwork pixels*, hand-verified against
# the image (see --hud-theme-check). Coordinates as data rather than as source
# is what makes the artwork replaceable: new art means a new theme.json, not a
# code change. Auto-detection off the artwork's own pixels finds the
# high-contrast recesses and the magenta-keyed sprites reliably, but a recess
# whose interior is close in brightness to the panel around it cannot be
# separated from it, so those were read by hand -- which is why the check tool
# exists at all.
#
# Project-relative, not CWD-relative: renders are run from a contest
# directory (`cd 26augusztus && uv run ../contest_video.py ...`), where a
# relative "hud-theme" would not exist.
HUD_THEME_DIR = str(PROJECT_ROOT / "hud-theme")
_THEME_OUTLINES = {  # group -> colour, matching what the overlay draws
    "slots": (0, 255, 255),
    "chips": (255, 140, 0),
    "stats": (255, 255, 0),
    "sprites": (0, 255, 0),
}


def load_hud_theme(path: str = HUD_THEME_DIR) -> dict:
    """Read a theme directory into {..., 'image': PIL.Image}."""
    with open(os.path.join(path, "theme.json")) as fh:
        theme = json.load(fh)
    theme["image"] = Image.open(
        os.path.join(path, theme.get("artwork", "artwork.png"))
    ).convert("RGB")
    return theme


def hud_theme_rects(theme: dict) -> list[tuple[str, tuple, tuple]]:
    """(group, colour, rect) for everything theme.json positions, flattened."""
    out = []
    for name, rect in theme.get("slots", {}).items():
        out.append((f"slots.{name}", _THEME_OUTLINES["slots"], tuple(rect)))
    for row, rects in theme.get("chips", {}).items():
        for i, rect in enumerate(rects):
            out.append((f"chips.{row}[{i}]", _THEME_OUTLINES["chips"], tuple(rect)))
    for i, rect in enumerate(theme.get("stats", [])):
        out.append((f"stats[{i}]", _THEME_OUTLINES["stats"], tuple(rect)))
    for name, sp in theme.get("sprites", {}).items():
        out.append((f"sprites.{name}", _THEME_OUTLINES["sprites"], tuple(sp["box"])))
    return out


def hud_theme_overlay(theme: dict) -> Image.Image:
    """The artwork with every rect in theme.json drawn onto it, and each
    needle's pivot marked -- the check for a hand-edited theme."""
    img = theme["image"].copy()
    draw = ImageDraw.Draw(img)
    bar = theme.get("bar")
    if bar:
        draw.rectangle(
            [bar[0], bar[1], bar[0] + bar[2] - 1, bar[1] + bar[3] - 1],
            outline=(255, 0, 255),
            width=3,
        )
    for name, colour, (x, y, w, h) in hud_theme_rects(theme):
        draw.rectangle([x, y, x + w, y + h], outline=colour, width=3)
        draw.text((x + 3, y + 3), name.split(".")[-1], fill=colour)
    for sp in theme.get("sprites", {}).values():
        if "pivot" in sp:
            px, py = sp["pivot"]
            draw.ellipse([px - 8, py - 8, px + 8, py + 8], outline=(255, 0, 0), width=3)
            draw.line([px - 12, py, px + 12, py], fill=(255, 0, 0), width=1)
            draw.line([px, py - 12, px, py + 12], fill=(255, 0, 0), width=1)
    return img


# The bar is drawn at the artwork's own aspect (1982x351 = 5.65:1) and never at
# any other: it is scaled uniformly or not at all, since squashing it to a
# different aspect turns the compass into an ellipse -- and the compass is the
# specific reason this artwork was chosen. 340px of a 1080p frame is 31% of its
# height, which the cast PiP (height-constrained when a HUD is present, see
# render) absorbs by shrinking.
HUD_W, HUD_H = 1920, 340
HUD_RED = (255, 48, 32)
HUD_AMBER = (255, 176, 32)
HUD_GREEN = (72, 255, 96)

_HUD_BANDS = ("2M", "70CM", "23CM")
_HUD_MODES = ("SSB", "CW", "FM")
# The artwork bakes the band/mode chips and the whole S-meter *lit*; whatever
# is not currently selected (or not currently reading) is dimmed in place
# rather than being a second, unlit asset that would have to be kept
# stylistically in sync with the lit one.
HUD_UNLIT_DIM = 0.15
HUD_SLOT_PAD = 0.06  # margin inside a recess, as a fraction of its short side
HUD_METER_SEGMENTS = 21  # LEDs in the meter sprite, counted off the artwork
# How far a needle reaches, as a fraction of the compass slot's radius. The
# sprites are not drawn to the rose's own scale (at 1:1 they overshoot the
# compass card entirely), so this is a fit, not a measurement: it lands the tip
# just inside the ring of N/E/S/W letters.
HUD_NEEDLE_FRAC = 0.75
# Physical cell counts, as a real instrument's display would have. A leading
# "1" is a half digit: the cell can only ever show a 1, which is what the unlit
# backdrop then advertises. Sized from real results -- the best single-round
# score seen in published Puskas logs is 8937, and QSO counts run to a few
# dozen -- so 4.5 digits of score (19999) and 2.5 of QSOs (199) have room to
# spare without wasting cells that would shrink every digit.
HUD_SCORE_FIELD = "18888"
HUD_QSOS_FIELD = "188"
# The QRG is fixed-width for a different reason: 23cm is 1296.174, one cell
# wider than 2m's 144.174, so without a field the digits resize on a band
# change mid-video. Its leading cell is a half digit too -- the highest band
# this radio has is 1296 MHz, so a thousands digit above 1 cannot occur.
HUD_QRG_FIELD = "1888.888"


def _key_magenta(img: Image.Image) -> Image.Image:
    """Cut a sprite out of the sheet's flat magenta background.

    The key is a hard threshold on "magenta-ness" (min(R,B) - G), which is
    large only for the background and at most zero for anything the sprites are
    actually made of -- red, orange, green, white highlights and the grey
    pivot ball all have G at least as high as one of R/B. A hard threshold
    rather than a soft alpha ramp because the sheet is not flat #FF00FF in
    practice (it carries generation/compression noise, and only 145 pixels of
    the whole image are exactly the key colour), so the edge pixels a ramp
    would keep semi-opaque are magenta-tinted and would read as a pink fringe.
    Keyed-out pixels are blacked as well as cleared so that resampling blends
    edges toward black -- the sprites already have black outlines, so the
    fringe that leaves is the outline itself."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.minimum(a[:, :, 0], a[:, :, 2]) - a[:, :, 1] > 60
    rgba = np.dstack([np.asarray(img.convert("RGB")), np.where(bg, 0, 255)])
    rgba[bg] = 0
    return Image.fromarray(rgba.astype(np.uint8), "RGBA")


@dataclass
class HudArt:
    """The artwork prepared for one bar size, so a render frame is a copy plus
    the values: the bar image itself, every theme.json rect scaled into it, and
    the sprites cut out, keyed and pre-scaled to where they get pasted.

    Needle pivots are in their own sprite's coordinates -- these needles turn
    about the ball at their base, not about their bounding box's centre."""

    bar: Image.Image
    slots: dict[str, tuple[int, int, int, int]]
    chips: dict[str, list[tuple[int, int, int, int]]]
    stats: list[tuple[int, int, int, int]]
    sprites: dict[str, Image.Image]
    pivots: dict[str, tuple[float, float]]


def hud_art(theme: dict, W: int = HUD_W, H: int = HUD_H) -> HudArt:
    """Scale a theme's artwork and coordinates to a W x H bar."""
    bx, by, bw, bh = theme["bar"]
    sx, sy = W / bw, H / bh

    def scaled(rect):
        x, y, w, h = rect
        return (
            round((x - bx) * sx),
            round((y - by) * sy),
            round(w * sx),
            round(h * sy),
        )

    art = theme["image"]
    bar = art.crop((bx, by, bx + bw, by + bh)).resize((W, H), Image.LANCZOS)
    slots = {name: scaled(r) for name, r in theme["slots"].items()}

    def cut(name: str) -> Image.Image:
        x, y, w, h = theme["sprites"][name]["box"]
        return _key_magenta(art.crop((x, y, x + w, y + h)))

    def fit(name: str, slot: str) -> Image.Image:
        return cut(name).resize(slots[slot][2:], Image.LANCZOS)

    sprites = {"rx": fit("rx", "lamp"), "tx": fit("tx", "lamp")}
    sprites["meter"] = fit("meter", "smeter")
    pivots = {}
    for name in ("needle", "target"):
        sp = theme["sprites"][name]
        px, py = sp["pivot"][0] - sp["box"][0], sp["pivot"][1] - sp["box"][1]
        # py is the sprite's own tip length: the tip sits at its top edge.
        scale = HUD_NEEDLE_FRAC * min(slots["compass"][2:]) / 2 / py
        img = cut(name)
        sprites[name] = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
        pivots[name] = (px * scale, py * scale)
    return HudArt(
        bar=bar,
        slots=slots,
        chips={r: [scaled(c) for c in cs] for r, cs in theme["chips"].items()},
        stats=[scaled(r) for r in theme["stats"]],
        sprites=sprites,
        pivots=pivots,
    )


def _inset(rect, frac: float = HUD_SLOT_PAD) -> tuple[int, int, int, int]:
    """A recess's usable interior: the readout must not sit hard against the
    frame the artwork drew around it."""
    x, y, w, h = rect
    d = round(min(w, h) * frac)
    return (x + d, y + d, w - 2 * d, h - 2 * d)


# Seven-segment digits come from DSEG7 (Debian's fonts-dseg, SIL OFL) rather
# than being drawn as polygons -- an earlier version built each segment by
# hand to avoid a font dependency, but the package is packaged, the glyphs are
# better than hand-rolled ones, and it removed ~120 lines of geometry.
# Unlit segments are drawn too, very dim: that is what makes an LED panel
# read as a panel rather than as numerals floating on black. Keep HUD_SEG_DIM
# low -- at 0.16 the ghost behind a '1' (which lights only its two right-hand
# bars) read as a digit being clipped by the panel edge rather than as an
# unlit cell.
# Vendored beside the artwork rather than taken from /usr/share/fonts: the HUD
# is unrenderable without it, and a system font package is one more thing that
# has to be installed on every machine that renders (CI included).
DSEG_FONT_PATH = os.path.join(HUD_THEME_DIR, "DSEG7Classic-Bold.ttf")
HUD_SEG_DIM = 0.12  # brightness of an unlit segment

_DSEG_FONTS: dict[int, ImageFont.FreeTypeFont] = {}


def _dseg_font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _DSEG_FONTS:
        _DSEG_FONTS[size] = ImageFont.truetype(DSEG_FONT_PATH, size)
    return _DSEG_FONTS[size]


def _all_segments(text: str) -> str:
    """`text` with every segment lit. '.' and ':' have no advance width of
    their own in DSEG7 (they overlay the preceding cell), so keeping them
    keeps the lit and unlit strings exactly the same width."""
    return "".join(ch if ch in ".: " else "8" for ch in text)


def _seven_seg(draw, text, x, y, max_w, max_h, colour, anchor="mm", field=None) -> None:
    """Draw `text` as segment digits, scaled down to fit max_w x max_h.

    `field` is the display's *physical* set of cells, e.g. "18888" for a
    four-and-a-half digit readout. Given one, the value is drawn right-aligned
    within it at a fixed size and position, so a score gaining a digit
    mid-round no longer resizes and reflows the whole panel -- which is both
    what a real instrument does and the only way the unlit backdrop can show
    the cells that aren't currently in use. A leading '1' is the half digit a
    real panel gives you for a leading 1 without paying for a full cell.

    Right-alignment is done by measuring the value rather than by padding it:
    DSEG7's space is only about a quarter of a cell wide, so a space-padded
    string does not line up with the field's own cells at all.

    Without a field, the all-lit form of the value serves as both backdrop and
    positioning reference -- a value containing '-' (the "--.-" placeholder)
    has a box only as tall as the middle segment, so anchoring on the value's
    own box would float the dashes above where the digits they replace sit."""
    if not text:
        return
    box = _all_segments(text)
    if field is not None and len(text) <= len(field):
        box = field
    # A leading "1" is a half digit: DSEG7 draws its two bars at the right of
    # the cell, so the left half is always blank. Charging a full cell of width
    # for it would shrink every other digit for nothing -- the visible extent
    # is half a cell narrower than the advance, and the string is drawn shifted
    # left by that much so the blank half falls outside the panel.
    half = box.startswith("1")
    size = max(6, round(max_h))
    while True:
        font = _dseg_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), box, font=font)
        w, h = right - left, bottom - top
        visible = w - (0.5 * draw.textlength("8", font=font) if half else 0.0)
        if size <= 6 or (visible <= max_w and h <= max_h):
            break
        size = max(6, int(size * 0.93))
    pad = 0.5 * draw.textlength("8", font=font) if half else 0.0
    vis = w - pad
    ax = x - vis / 2 - pad if anchor == "mm" else x - w if anchor == "rm" else x
    ay = y - h / 2 - top
    draw.text(
        (ax, ay), box, font=font, fill=tuple(round(c * HUD_SEG_DIM) for c in colour)
    )
    draw.text(
        (ax + w - draw.textlength(text, font=font), ay), text, font=font, fill=colour
    )


def _paste_needle(img: Image.Image, sprite, pivot, centre, az: float) -> None:
    """Paste a compass needle pointing at `az` degrees (0 = north, clockwise),
    turned about its own pivot -- the ball at its base, well below the middle
    of its bounding box, so rotating about the box centre would swing the whole
    needle around the compass instead of pointing it.

    Done by padding the sprite into a square canvas centred on that pivot,
    which turns "rotate about an arbitrary point" into PIL's own "rotate about
    the centre"."""
    px, py = pivot
    r = math.ceil(
        max(
            math.hypot(dx - px, dy - py)
            for dx in (0, sprite.width)
            for dy in (0, sprite.height)
        )
    )
    canvas = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    canvas.alpha_composite(sprite, (r - round(px), r - round(py)))
    turned = canvas.rotate(-az, resample=Image.BILINEAR)  # PIL turns the other way
    img.paste(turned, (round(centre[0]) - r, round(centre[1]) - r), turned)


# --- 5x7 dot-matrix font, for the CW ticker -------------------------------
#
# Drawn dot by dot, with the same lit/unlit treatment as the segment panels
# above. Written out as a table rather than taken from a font file: the glyph
# set is tiny and fully determined (MORSE can only ever decode to these 44
# characters plus space), and at 5x7 a table is directly readable in the
# source -- each row of a glyph is visible as it will be drawn. Rendered as a
# sheet and eyeballed, since a mistyped row is a plausible-looking glyph
# rather than an error.
_FONT_5X7 = {
    " ": "00000 00000 00000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    ",": "00000 00000 00000 00000 00110 00100 01000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 00110 00110",
    "/": "00001 00010 00010 00100 01000 01000 10000",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    "=": "00000 00000 11111 00000 11111 00000 00000",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
}


def _matrix_rows(ch: str) -> list[str]:
    return _FONT_5X7.get(ch, _FONT_5X7["?"]).split()


def _draw_matrix_text(draw, cells, rect, colour, width_chars) -> None:
    """Draw a `width_chars`-wide 5x7 dot-matrix display.

    Every dot is drawn -- lit ones in `colour`, the rest at HUD_SEG_DIM -- so
    an idle display still reads as a display. `cells` are (column offset,
    character) pairs from HudTimeline.at; offsets are whole dot columns, and a
    character partly past either edge is simply clipped there, which is how
    text scrolls onto and off a real panel."""
    x, y, w, h = rect
    cols = max(1, width_chars * HUD_TICKER_CELL_COLS - 1)
    # Integer pitch and dot size, not fractional: at fractional values PIL
    # rounds each rectangle independently, so gaps come out a pixel wide in
    # some columns and zero in others and the display stops reading as a grid.
    pitch = max(2, int(min(w / cols, h / HUD_MATRIX_ROWS)))
    dot = max(1, pitch - max(1, round(pitch * 0.18)))
    ox = x + (w - pitch * cols) // 2
    oy = y + (h - pitch * HUD_MATRIX_ROWS) // 2
    dim = tuple(round(c * HUD_SEG_DIM) for c in colour)

    def put(col: int, row: int, fill) -> None:
        if 0 <= col < cols:
            dx, dy = ox + col * pitch, oy + row * pitch
            draw.rectangle([dx, dy, dx + dot - 1, dy + dot - 1], fill=fill)

    for col in range(cols):
        for row in range(HUD_MATRIX_ROWS):
            put(col, row, dim)
    for offset, ch in cells:
        for row, bits in enumerate(_matrix_rows(ch)):
            for c, bit in enumerate(bits):
                if bit == "1":
                    put(offset + c, row, colour)


def _dim_region(img: Image.Image, rect, factor: float) -> None:
    """Darken one rectangle in place -- how an unselected band/mode chip and
    the unreached part of the S-meter are made. Both are baked (and pasted)
    *lit*, and dimmed back rather than being a separate unlit pair of assets,
    so nothing has to stay stylistically in sync."""
    x, y, w, h = rect
    box = (x, y, x + w, y + h)
    img.paste(img.crop(box).point(lambda v: round(v * factor)), box)


def _seg_in(draw, rect, text, colour, field=None, right=False) -> None:
    """Draw a segment readout into a recess, fitted to it. Right-aligned for
    the stats rows, whose values change width; centred everywhere else."""
    x, y, w, h = _inset(rect)
    if right:
        _seven_seg(draw, text, x + w, y + h // 2, w, h, colour, anchor="rm")
    else:
        _seven_seg(draw, text, x + w // 2, y + h // 2, w, h, colour, field=field)


def draw_hud_frame(state: HudState, art: HudArt) -> Image.Image:
    """Render one HUD bar: the artwork, plus this instant's values.

    Nothing static is drawn -- every label, frame and the compass rose come
    from the artwork itself (see HudArt), so this only ever paints readouts,
    sprites and dimming."""
    img = art.bar.copy()
    draw = ImageDraw.Draw(img)

    # --- SCORE (DOOM's health): the biggest number on the bar, flashing as
    # it counts up after each QSO.
    colour = tuple(
        round(c + (255 - c) * state.score_flash) for c in HUD_RED
    )  # washes toward white at the moment of a QSO
    _seg_in(draw, art.slots["score"], f"{state.score}", colour, field=HUD_SCORE_FIELD)
    _seg_in(draw, art.slots["qsos"], f"{state.qsos}", HUD_RED, field=HUD_QSOS_FIELD)

    # --- QRG, then dim each band/mode chip to its lamp's current brightness.
    # Fully lit is the artwork as baked, so only a chip short of that is
    # touched at all -- which on a settled bar is every chip but the two in
    # use, exactly as when this was a straight lit/unlit test.
    qrg = f"{state.freq_hz / 1e6:.3f}" if state.freq_hz else "---.---"
    _seg_in(draw, art.slots["freq"], qrg, HUD_AMBER, field=HUD_QRG_FIELD)
    for row, names in (("band", _HUD_BANDS), ("mode", _HUD_MODES)):
        for rect, name in zip(art.chips[row], names):
            glow = state.chip_glow.get(name, 0.0)
            if glow < 1.0:
                _dim_region(img, rect, HUD_UNLIT_DIM + (1 - HUD_UNLIT_DIM) * glow)

    # --- RX/TX lamp. With no rig state at all the socket is simply left
    # empty, which is what the artwork already draws there.
    if state.ptt is not None:
        lamp = art.sprites["tx" if state.ptt else "rx"]
        img.paste(lamp, art.slots["lamp"][:2], lamp)

    # --- signal meter: the sprite is a fully lit LED bar, pasted over the
    # recess and then dimmed back from the current level rightwards, so lit and
    # unlit LEDs are the same artwork rather than two assets. Both the sprite
    # box and the slot hold the LED strip *itself*, with the frame around it
    # left to the artwork -- so the cut is simply lit/segments of the width,
    # and it lands in a gap rather than through an LED (a half-lit segment
    # reads as a rendering fault rather than as a reading). An earlier crop
    # took in the frame too, which then got dimmed along with the LEDs.
    x, y, w, h = art.slots["smeter"]
    img.paste(art.sprites["meter"], (x, y), art.sprites["meter"])
    lit = 0 if state.s_level is None else round(state.s_level * HUD_METER_SEGMENTS)
    if lit < HUD_METER_SEGMENTS:
        cut = x + round(w * lit / HUD_METER_SEGMENTS)
        _dim_region(img, (cut, y, x + w - cut, h), HUD_UNLIT_DIM)

    # --- compass: solid needle = where the rotator points, hollow needle =
    # bearing to the station being worked, so the swing onto target is visible.
    cx, cy, cw, ch = art.slots["compass"]
    centre = (cx + cw / 2, cy + ch / 2)
    # The hollow one goes on top: the two coinciding is the normal case, and
    # underneath the solid needle its outline would simply be invisible, so
    # "on target" would look identical to "no target known".
    for name, az in (("needle", state.rot_az), ("target", state.target_az)):
        if az is not None:
            _paste_needle(img, art.sprites[name], art.pivots[name], centre, az)

    # --- PWR: supply volts + PA current. No recording carries these yet --
    # the radio only reports them when polled, which the logger doesn't do --
    # so this renders placeholders rather than hiding, which would leave two
    # empty recesses on the bar looking like a fault.
    for name, value in (("vd", state.vd), ("id", state.id_a)):
        _seg_in(
            draw,
            art.slots[name],
            f"{value:.1f}" if value is not None else "--.-",
            HUD_RED,
        )

    # --- stats: values only, right-aligned. Their captions (UTC / RATE /H /
    # ODX KM) are part of the artwork, printed beside these recesses.
    for rect, value in zip(
        art.stats,
        (
            state.utc.strftime("%H:%M:%S") if state.utc else "--:--:--",
            f"{state.rate_per_h:.0f}",
            f"{state.best_km}",
        ),
    ):
        _seg_in(draw, rect, value, HUD_RED, right=True)

    # --- CW ticker: a fixed HUD_TICKER_CHARS-wide dot-matrix display, with
    # characters entering at the right edge as they are keyed. Uninset,
    # unlike every other readout: its slot is only seven dots tall to begin
    # with, so a margin there costs a whole dot of pitch. _draw_matrix_text
    # centres the grid in whatever it is given.
    _draw_matrix_text(
        draw, state.ticker, art.slots["ticker"], HUD_GREEN, HUD_TICKER_CHARS
    )
    return img


def hud_demo_state() -> HudState:
    """The mockup's own dummy values -- for --hud-demo, so the layout can be
    checked against the artwork with no recording at hand."""
    return HudState(
        t=0.0,
        utc=datetime(2026, 8, 3, 18, 42, 7),
        score=12847,
        qsos=63,
        rate_per_h=47,
        best_km=782,
        freq_hz=144_174_000,
        mode="CW",
        band="2M",
        chip_glow={"2M": 1.0, "CW": 1.0},
        ptt=False,
        rot_az=135,
        target_az=118,
        s_level=0.62,
        ticker=[(i * HUD_TICKER_CELL_COLS, c) for i, c in enumerate("TU 5NN JN86SR")],
        vd=13.8,
        id_a=12.4,
    )


HUD_H_FRAC = HUD_H / 1080  # bar height as a fraction of the frame, from the
# 1080p reference layout; scaled for other resolutions.


def hud_height(H: int) -> int:
    """The bar's pixel height for a given frame height, forced even.

    libx264 refuses an odd dimension, and 720p lands on 173 -- found only by
    rendering an actual clip at 720p, since the 1080p reference height is
    already even and every string-level test used it. One function rather than
    the same rounding in main() and render(), which must agree exactly or the
    bar is scaled to a different height than it was drawn at."""
    return 2 * round(H * HUD_H_FRAC / 2)


def hud_frame_key(state: HudState) -> tuple:
    """What the HUD's pixels actually depend on, for frame reuse.

    Everything except `t` itself, with the continuously-varying values
    quantised to the resolution they are *drawn* at: the meter is 18 discrete
    segments and a needle rounded to the nearest degree moves well under a
    pixel. Without this the scope-derived signal level alone would force a
    fresh draw ~30 times a second."""
    return (
        state.utc.replace(microsecond=0) if state.utc else None,
        state.score,
        round(state.score_flash, 2),
        state.qsos,
        round(state.rate_per_h),
        state.best_km,
        state.freq_hz,
        # Quantised like the meter and the needle: the chips are dimmed in
        # 8-bit steps, so a ramp finer than that redraws for nothing.
        tuple(sorted((n, round(g, 2)) for n, g in state.chip_glow.items())),
        state.ptt,
        None if state.rot_az is None else round(state.rot_az),
        None if state.target_az is None else round(state.target_az),
        None if state.s_level is None else round(state.s_level * 18),
        tuple(state.ticker),
        None if state.vd is None else round(state.vd, 1),
        None if state.id_a is None else round(state.id_a, 1),
    )


def render_hud_video(
    timeline: HudTimeline,
    out_path: str,
    art: HudArt,
    duration: float,
    fps: int = RENDER_FPS,
) -> int:
    """Render the HUD bar to its own clip, to be composited by render().

    Same separate-stage-then-composite pattern as render_cast_video and
    render_scope_video: PIL frames piped straight into ffmpeg as rawvideo,
    no intermediate PNGs. Its t=0 is the output timeline's t=0, so render()
    needs no -itsoffset for it at all -- unlike every other side stream here,
    this one is generated *from* that timeline rather than captured against
    an independent clock.

    Returns the number of frames actually drawn, which is what the reuse
    optimisation is measured by."""
    W, H = art.bar.size
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", f"{fps}",
        "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", out_path,
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    drawn = 0
    try:
        last_key = None
        frame = None
        total = max(1, int(duration * fps))
        with stage_bar("HUD", total) as bar:
            for i in range(total):
                state = timeline.at(i / fps)
                key = hud_frame_key(state)
                if key != last_key or frame is None:
                    frame = draw_hud_frame(state, art).tobytes()
                    last_key = key
                    drawn += 1
                proc.stdin.write(frame)
                bar.update()
    finally:
        proc.stdin.close()
        proc.wait()
    return drawn
