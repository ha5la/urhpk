"""The terminal PiP: an asciinema .cast replayed into video frames.

Rendered as a real video rather than as ASS text, since the cast can contain
many more state changes per second than are worth a separate subtitle event
(the toolbar clock alone ticks ~10x/second the whole round), and a fixed,
modest frame rate reads perfectly well for text. pyte replays the cast into a
virtual terminal; each frame is rasterized with PIL.

Sync is exact and needs no cross-correlation at all, unlike the webcam:
asciinema's own cast v2 format embeds a Unix-epoch "timestamp" in its header,
recorded by the same machine's clock that (if the logger is also running
there) already drives every other precise timestamp in the pipeline -- see
recorders.py's webcam capture for the same reasoning.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pyte
from PIL import Image, ImageDraw, ImageFont

CAST_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
CAST_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
CAST_FONT_SIZE = 13
CAST_FPS = 10.0  # text doesn't need video frame rates to read cleanly
CAST_BG = (10, 10, 10)
CAST_PALETTE = {
    "default": (220, 220, 220),
    "black": (0, 0, 0),
    "red": (205, 49, 49),
    "green": (13, 188, 121),
    "brown": (229, 229, 16),
    "blue": (36, 114, 200),
    "magenta": (188, 63, 188),
    "cyan": (17, 168, 205),
    "white": (229, 229, 229),
    "brightblack": (102, 102, 102),
    "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139),
    "brightbrown": (245, 245, 67),
    "brightblue": (59, 142, 234),
    "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219),
    "brightwhite": (255, 255, 255),
}


def parse_cast_header(path: str) -> tuple[datetime, int, int]:
    """(start_utc, width, height) from an asciinema cast v2 file's header
    line. The embedded Unix-epoch `timestamp` is exact, real-world UTC --
    no local-timezone ambiguity the way a filename-embedded wall-clock
    string has (see parse_webcam_wall), so no derive_utc_offset-style
    whole-hour rounding is needed here at all."""
    with open(path) as f:
        header = json.loads(f.readline())
    start_utc = datetime.fromtimestamp(header["timestamp"], tz=timezone.utc).replace(
        tzinfo=None
    )
    return start_utc, header["width"], header["height"]


def _cast_color(
    name: str | None, default: tuple[int, int, int]
) -> tuple[int, int, int]:
    if name is None or name == "default":
        return default
    return CAST_PALETTE.get(name, default)


def _draw_cast_row(
    draw: ImageDraw.ImageDraw,
    line: dict,
    row: int,
    W: int,
    font: ImageFont.FreeTypeFont,
    font_b: ImageFont.FreeTypeFont,
    cw: float,
    lh: int,
) -> None:
    """Redraw one terminal row onto `draw`'s canvas -- erasing the row
    first (a plain background rectangle) is essential, not an optimization:
    without it, a cell that goes from non-blank to blank (e.g. a shorter
    string overwriting a longer one) would leave stale pixels behind,
    since we only ever redraw rows pyte marks dirty, never the whole
    canvas from scratch after the first frame."""
    y = row * lh
    draw.rectangle([0, y, int(cw * W) + 4, y + lh], fill=CAST_BG)
    for col in range(W):
        ch = line[col]
        if ch.data == " " and ch.bg in (None, "default") and not ch.reverse:
            continue
        fg = _cast_color(ch.fg, CAST_PALETTE["default"])
        bg = _cast_color(ch.bg, CAST_BG)
        if ch.reverse:
            fg, bg = bg, fg
        x = int(col * cw)
        if bg != CAST_BG:
            draw.rectangle([x, y, x + cw, y + lh], fill=bg)
        f = font_b if ch.bold else font
        draw.text((x, y), ch.data, font=f, fill=fg)


class _CastScreen(pyte.Screen):
    """pyte.Screen plus the horizontal-margin scrolling stock pyte omits.

    A logger cast recorded inside tmux (two panes side by side) needs this:
    tmux scrolls/clears a *single* pane by setting left/right margins (DECSLRM,
    `CSI Pl;Pr s`) and then scrolling within them (SU `CSI Ps S` / SD
    `CSI Ps T`). Stock pyte implements none of these three, so a pane was never
    actually cleared -- when the logger cleared its screen and redrew shorter
    content, the old tail stayed on screen (the "startup screen still visible
    behind the contest screen" garbage). A real terminal -- and `asciinema
    play` -- honours them, which is why the cast looked clean there but not in
    our render. (This corrects an earlier diagnosis that blamed the logger's
    own redraw for omitting erase-to-end-of-line: the erase is really tmux's
    SU+DECSLRM, which we were dropping.)"""

    def reset(self) -> None:
        super().reset()
        self.margins_lr: tuple[int, int] | None = None

    def set_left_right_margins(
        self, left: int = 0, right: int | None = None, **kwargs
    ) -> None:
        # `CSI s` with <2 params is SCOSC (save cursor), not DECSLRM.
        if right is None:
            self.save_cursor()
            return
        self.margins_lr = ((left or 1) - 1, (right or self.columns) - 1)

    def _scroll(self, count: int, down: bool) -> None:
        count = count or 1
        top, bottom = self.margins if self.margins else (0, self.lines - 1)
        left, right = self.margins_lr or (0, self.columns - 1)
        self.dirty.update(range(top, bottom + 1))
        blank = self.default_char
        rows = range(bottom, top - 1, -1) if down else range(top, bottom + 1)
        for y in rows:
            src = y - count if down else y + count
            srow = self.buffer[src] if top <= src <= bottom else None
            row = self.buffer[y]
            for x in range(left, right + 1):
                row[x] = srow[x] if srow is not None else blank

    def scroll_up(self, count: int = 0, **kwargs) -> None:
        self._scroll(count, down=False)

    def scroll_down(self, count: int = 0, **kwargs) -> None:
        self._scroll(count, down=True)

    def index(self) -> None:
        # Stock pyte's index()/reverse_index() (used for a *plain* linefeed
        # that lands on the bottom/top margin row -- not just explicit SU/SD)
        # replace the whole row object (`self.buffer[y] = self.buffer[y+1]`),
        # ignoring margins_lr entirely. Found from the real cast: irssi's
        # pane fills up and a plain '\n' at its own bottom margin auto-
        # scrolled -- and dragged the *logger's* pane (outside DECSLRM)
        # up with it, eventually scrolling its title off screen. Confirmed
        # by direct reproduction: with stock pyte's index(), a bottom-margin
        # linefeed restricted to the left pane's columns (via DECSLRM) still
        # shifted the right pane's rows. Delegating to our own _scroll (which
        # already respects margins_lr, see scroll_up/scroll_down above) fixes
        # both the explicit-SU and the plain-linefeed paths the same way.
        top, bottom = self.margins if self.margins else (0, self.lines - 1)
        if self.cursor.y == bottom:
            self._scroll(1, down=False)
        else:
            self.cursor_down()

    def reverse_index(self) -> None:
        top, bottom = self.margins if self.margins else (0, self.lines - 1)
        if self.cursor.y == top:
            self._scroll(1, down=True)
        else:
            self.cursor_up()


class _CastStream(pyte.ByteStream):
    """pyte.ByteStream that routes the three CSI finals _CastScreen adds
    (SU/SD/DECSLRM) -- stock pyte's dispatch table has no entry for them."""

    csi = {
        **pyte.ByteStream.csi,
        "S": "scroll_up",
        "T": "scroll_down",
        "s": "set_left_right_margins",
    }


def render_cast_video(
    cast_path: str,
    out_path: str,
    fps: float = CAST_FPS,
    max_duration: float | None = None,
) -> None:
    """Replay an asciinema cast into a standalone mp4 (its own timeline
    starting at t=0, matching the cast's own start) -- an intermediate
    file in the same spirit as concat_audio's wav, so the main render()
    just treats it as one more PIP video input alongside the webcam.

    Frames are piped as raw RGB24 straight into an ffmpeg encode, not
    written to disk as a PNG sequence first -- a multi-hour round at
    even a modest fps would otherwise mean tens of thousands of files.

    The canvas persists across frames and only pyte's own `screen.dirty`
    rows are redrawn each tick, not the whole screen -- a real terminal
    is mostly static between two samples 100ms apart (one QSO row, or
    just the toolbar clock, changes at a time), and redrawing every one
    of a wide terminal's cells every frame regardless measured at under
    1x realtime throughput, worse than the encode itself for a multi-hour
    round."""
    with open(cast_path) as f:
        header = json.loads(f.readline())
        events = [json.loads(line) for line in f]
    W, H = header["width"], header["height"]
    duration = events[-1][0] if events else 0.0
    # A --duration preview only shows the first minutes of a round, so
    # replaying the whole cast is wasted work -- and it is the slowest stage
    # in the pipeline, so it dominates exactly the case the flag exists to
    # make cheap. Measured: a 20-minute cut of a 121-minute round spent
    # ~40 minutes here to use ~7 of it.
    if max_duration is not None:
        duration = min(duration, max_duration)

    font = ImageFont.truetype(CAST_FONT_PATH, CAST_FONT_SIZE)
    font_b = ImageFont.truetype(CAST_FONT_BOLD, CAST_FONT_SIZE)
    cw = font.getlength("M")
    # A fixed 1.2x-of-size guess (15px at CAST_FONT_SIZE=13) undershot this
    # font's real metrics (ascent+descent=17px) -- descenders like '_' or
    # 'g' rendered fine on their *own* row but got clipped the next time
    # the row *below* was erased-and-redrawn (see _draw_cast_row, which
    # only ever clears exactly one row's own rectangle), since 2px of
    # every glyph's descender was actually spilling into that next row's
    # territory. Real reported case: a static irssi banner's underscores
    # visibly disappeared partway through a render, despite the row itself
    # never changing again -- confirmed by comparing the pre-encode canvas
    # directly (not a video-compression artifact) against the exact same
    # pyte state rendered without the per-row erase rectangle at all.
    ascent, descent = font.getmetrics()
    lh = ascent + descent
    # even dimensions: libx264/yuv420p chroma subsampling requires it
    px_w, px_h = int(cw * W) + 4, lh * H + 4
    px_w += px_w % 2
    px_h += px_h % 2

    screen = _CastScreen(W, H)
    stream = _CastStream(screen)
    canvas = Image.new("RGB", (px_w, px_h), CAST_BG)
    draw = ImageDraw.Draw(canvas)
    for row in range(H):
        _draw_cast_row(draw, screen.buffer[row], row, W, font, font_b, cw, lh)
    screen.dirty.clear()

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{px_w}x{px_h}",
        "-r",
        f"{fps}",
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        ei = 0
        n = len(events)
        t = 0.0
        dt = 1.0 / fps
        while t <= duration:
            while ei < n and events[ei][0] <= t:
                ts, kind, data = events[ei]
                if kind == "o":
                    stream.feed(data.encode())
                ei += 1
            for row in screen.dirty:
                _draw_cast_row(draw, screen.buffer[row], row, W, font, font_b, cw, lh)
            screen.dirty.clear()
            proc.stdin.write(canvas.tobytes())
            t += dt
    finally:
        proc.stdin.close()
        proc.wait()
