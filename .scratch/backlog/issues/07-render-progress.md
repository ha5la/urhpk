# 07 — Progress reporting for the render

Status: resolved

`contest_video.py` runs for hours unattended with little indication of where
it is. Add per-stage progress with `tqdm`.

The render is several sequential stages with very different rates, so a single
undifferentiated bar would mislead. The cast replay is the slowest: a 20-minute
cut of a 121-minute round spent ~40 minutes in it.

## Which stages

Four, each a bounded loop that already knows its own total:

| stage | total |
|---|---|
| `render_cast_video` (`cast_render.py:193`) | `duration * fps`, already clamped by `max_duration` |
| `render_hud_video` (`hud_draw.py:632`) | `int(duration * fps)`, the loop's own range |
| `render_scope_video` (`scope_render.py:60`) | rows on the fixed time grid |
| `decode_round` (called at `contest_video.py:764`) | segments |

Each bar lives in the function that owns the loop.

**Not the final ffmpeg pass**: `render()` already passes `-stats`
(`contest_video.py:182`), so ffmpeg prints its own progress line — replacing it
would mean parsing `-progress pipe:1` to reproduce what already works. Not
`concat_audio` either: one ffmpeg call over known input, not where the hours go.

## Both a terminal and a log

The unattended runs redirect (`2026-aug/render.log` is a real artifact of one),
so the non-tty case is not hypothetical. One knob rather than a second rendering
path: `mininterval=30` when `not sys.stderr.isatty()`, which makes tqdm emit a
newline-terminated line every ~30 s — readable in a log, and the tty case keeps
the in-place bar.

The ETA is the point of the ticket: "where is it" is really "when will it
finish", and rate smoothing over a 40-minute cast replay is the part that would
otherwise be reimplemented badly. That is what buys `tqdm` its place among the
four runtime dependencies.

## Tests

Assert the frame loops still emit byte-identical output. The bars themselves are
not tested and get no injected progress factory — an abstraction that would exist
only to be asserted against, wrapping the already-tested thing.

## Answer

Built as described. `urhpk/progress.py` holds `stage_bar`, whose only decision —
redraw rate against `isatty` — is what `tests/test_progress.py` pins.

**Byte-identity was checked, but not as a committed test.** A golden hash pins
the ffmpeg build, which is the reason 18 says a differential render cannot run in
CI; the same argument applies to a unit test asserting one. So it was measured
instead: the full `test/` round rendered at 720p before and after, with
`--keep-intermediates`, and all five artifacts came out identical —
`cast.mp4 e2b71827`, `hud.mp4 25df7038`, `scope.mp4 5290df01`,
`concat.wav 21e718e9`, `out.mp4 4738d097`. 5m32s before, 5m26s after, so the
bars cost nothing measurable.

Two things the ticket did not foresee:

**The stage announcements were arriving after their own stage.** `print` goes to
block-buffered stdout when redirected, while tqdm and ffmpeg's `-stats` write to
stderr unbuffered — in the real `render.log`, `rendering (this takes a while)
...` sat *below* ffmpeg's output. `main` now puts stdout in line-buffered mode.
The four `print("rendering X ...")` lines a bar's own description replaces are
gone; the summary lines after each stage stay.

**tqdm keeps using `\r` off a terminal too**, so a stage leaves one long line
rather than one line per update. With `mininterval=30` that is a handful of
updates, `tail -f` renders them live, and the finished log holds each stage's
last state — good enough, and the docstring says what actually happens rather
than what was assumed.

The ticket's claim that the cast replay is the slowest stage is round-dependent:
on `test/`, which has a dense `.scope`, the waterfall took 1m45s against the cast
replay's 32s and the HUD's 41s.
