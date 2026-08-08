# HUD artwork generation prompt

The prompt used to generate the HUD background artwork consumed by
`contest_video.py --hud-background`. Kept in the repo because it is painful to
reconstruct later, and because regenerating the asset means re-running *this*
text, not improvising a new one.

## How to run it

Paste it as a **standalone prompt** (it deliberately refers to nothing outside
itself), but run it in the **same chat session as the previous artwork** so the
generator also has that image as visual context. Those are two different goals:
self-contained text is what makes it reproducible a year from now, while the
session history is what keeps the style consistent with the version already
approved.

## What the software draws, and therefore what the artwork must not

Everything that moves is drawn at render time and must be left as an empty
recess in the artwork:

| Element | Drawn by |
|---|---|
| Score, QSOs, QRG, volts, amps, bearing, UTC, rate, ODX | DSEG7 segment font |
| — with fixed cell counts: score 4½, QSOs 2½, QRG 7½ digits | `HUD_*_FIELD` |
| Every fixed label and caption, including "UTC" / "RATE /H" / "ODX KM" | the artwork itself |
| CW ticker | 5x7 dot-matrix font, `_FONT_5X7` |
| Band/mode chips | baked **lit** in the artwork, dimmed per-pixel when inactive |
| RX/TX lamp, S-meter fill, both compass needles | sprites cut from the bottom region |
| Webcam | composited by ffmpeg into the face recess |

The bottom region's flat magenta background is what makes sprite extraction
automatic: non-magenta pixels are sprite, magenta is not, so each sprite's
bounding box is found by connected-component detection and identified by its
left-to-right order. That is why the sprite order in the prompt is fixed and why
each sprite needs a generous magenta margin.

A coordinate table baked into the image was considered and rejected: an image
generator cannot measure its own output raster, so any such table would be
confabulated while looking authoritative.

## The prompt

```
Create a single image containing a ham radio contest HUD in the visual style of
the original DOOM (1993) status bar, plus a small set of separate UI pieces
below it that will later be composited into that HUD by software.

STYLE (applies to everything): dark grey-brown embossed metal with a subtle
grit texture. Chunky rectangular recessed panels with hard 2-pixel bevelled
edges, light top-left and dark bottom-right. Small labels in pale grey
uppercase pixel lettering. Flat front-on 2D interface art — no perspective, no
photography, no 3D rendering, no gradients, no glow, no drop shadows.

The image has two clearly separate regions: the HUD bar across the top, and
below it a plain flat asset sheet holding loose UI pieces. They must not blend
into each other — the asset sheet is a flat background with pieces on it, not
a continuation of the metal panel.

TOP REGION — the HUD bar. A very wide, very shallow letterbox strip spanning
the full width of the image: roughly 7.4 times wider than it is tall, for
example 1920 x 260 pixels. This is much wider and flatter than a typical
control panel, so if in doubt make the bar thinner rather than taller. It
contains these panels in one horizontal row, left to right:

SIZING THE EMPTY RECESSES: each value recess below is given the widest reading
it will ever have to display, written out. Size the recess so that reading
would fit comfortably, then LEAVE IT EMPTY — those sample numbers are size
references, not content, and must not appear anywhere in the image. A leading
"1" means a half-width digit position: that cell can only ever show a 1, so it
takes about half the width of a full digit.

1. Wide panel, empty dark recess, small label "SCORE" centred at its bottom.
   Size the recess for "18888". These are the largest numerals on the whole
   bar.
2. Narrower panel, empty dark recess, label "QSOS" at its bottom. Size it for
   "188" at the same digit size as SCORE, making it a little over half as
   wide as panel 1.
3. Panel whose empty dark recess is sized for "1888.888", in digits somewhat
   smaller than SCORE's, with the small label "MHz" under it; below that, two
   rows of small rectangular selector chips reading
   [2M][70CM][23CM] and [SSB][CW][FM]. Draw ALL SIX chips brightly lit in
   amber — none dark.
4. Narrow panel: an empty circular recess in the upper half, and below it a
   horizontal bar-meter slot showing its individual segment divisions but with
   every segment dark and unlit, with the small label "S" beneath.
5. A tall portrait rectangular recess with a heavy bevelled frame, its inside
   flat dark grey and completely empty. This is where a webcam image will go.
6. Panel with a circular compass rose showing only the letters N, E, S and W
   around its edge and NO needle or pointer of any kind. Below the circle,
   empty space, then the small label "ROT".
The last three panels form a two-row block occupying the bar's full height,
so read all three before drawing any of them: panels 7 and 8 sit side by side
across the UPPER HALF only, and panel 9 spans the full width of both of them
across the LOWER HALF. None of the three is full height, and none overlaps
another.

7. Upper half only. Narrow panel with two empty value slots stacked
   vertically, each sized for "88.8", each with a small label to its right —
   "V" for the upper, "A" for the lower — and the label "PWR" at the bottom
   of the panel.
8. Upper half only, immediately to the right of panel 7 and about twice its
   width. Three rows, each with a small grey caption on the left and an empty
   dark value area on the right; size those for "88:88:88". The captions are
   "UTC", "RATE /H" and "ODX KM".
9. Lower half only, directly beneath panels 7 and 8 and spanning the combined
   width of both. A wide, generously tall empty dark slot with the small label
   "CW" beneath it. This is a dot-matrix display whose dots are drawn by
   software, so leave its interior completely flat and empty — no dot grid, no
   pixel matrix, no characters, no texture of any kind inside it. Make it as
   tall as the lower half allows: at least 16 characters must fit side by side
   while each stays clearly legible, and if those two pull against each other,
   favour a taller slot with fewer characters over a shallow one with more.

CRITICAL: no readout anywhere in the HUD bar may show a value. Every value
area — score, QSO count, frequency, the two PWR slots, the compass reading,
the three right-hand rows and the CW slot — is an empty dark recess with
nothing in it. The only text anywhere in the bar is the fixed labels and
captions listed above, which do include the digits inside the band chips
("2M", "70CM", "23CM") and the captions "RATE /H" and "ODX KM". Those are
permanent labels, not values, and must appear.

BOTTOM REGION — an asset sheet, well separated from the bar above it: five
loose pieces sitting on a completely flat solid magenta (#FF00FF) background,
with no panel, frame, texture or shading behind them. Evenly spaced in one
row, in exactly this order, each surrounded by a generous margin of plain
magenta so it can be cut out cleanly:

1. A round lamp glowing bright green with the word "RX" in pale grey pixel
   lettering directly above it. This will be pasted into the empty circular
   recess of panel 4, so draw it to sit inside that recess rather than as a
   standalone control with its own frame.
2. The same piece again but with the lamp glowing bright red and the word "TX"
   above it. Identical size, position and styling to piece 1 — these two are
   alternate states of the same indicator.
3. A horizontal segmented LED bar meter, completely full with every segment
   lit, running green on the left through yellow to red on the right. This
   will be pasted over the empty meter slot in panel 4, so match that slot's
   proportions.
4. A compass needle to be overlaid on the compass rose of panel 6: a solid
   bright red tapered pointer, pointing straight up, drawn so that its pivot
   end is exactly at the centre of its square area and its length suits that
   rose.
5. A second needle, identical in size, pivot placement and orientation to
   piece 4, but drawn as a hollow amber outline instead of solid. It is the
   same rose's second pointer, so the two must be visually distinguishable
   when overlapping.

Do NOT draw any coordinate table, measurements, captions, numbering or
annotation anywhere in the image. The bottom region contains only the five
pieces on plain magenta.

To summarise what must NOT appear: no numbers in any recess of the HUD bar; no
needle, pointer or marking inside the compass rose; no lamp inside the
circular recess; no lit segments in the bar meter; nothing inside the tall
portrait webcam recess; nothing inside the CW slot; and no dark or unlit band
or mode chips — all six are lit.
```

## What matters, and what does not

Panel proportions do **not** need to be right. `HUD_SLOTS` is measured from
whatever artwork arrives, so a panel 30px wider than specified costs nothing.

What cannot be recovered afterwards, and is therefore what to check before
accepting a generation:

- **The bar's aspect ratio.** The first generation came back at 4.4:1 against a
  requested 8.7:1. Squashing that into 7.4:1 vertically crushes every baked
  label, and a bar taller than ~260px at 1920 wide collides with the terminal
  PiP above it. This is the one worth re-rolling for.
- **Values baked into recesses**, a needle drawn on the compass rose, a lit
  meter, or anything inside the webcam or CW slots — all permanently in the
  way of what the software draws there.
- **Any of the six band/mode chips drawn dark.** They are dimmed at render
  time, which only works if all six start lit.
- **A recess too small for its widest reading.** The readouts are fixed-width
  (a score gaining a digit must not resize the panel), so a recess sized by eye
  for three digits forces every digit smaller for the whole video. The prompt
  gives each one its widest value verbatim — `18888`, `188`, `1888.888`,
  `88.8`, `88:88:88` — which come from `HUD_SCORE_FIELD` and friends; keep the
  two in step if those ever change. The half digit is real: a leading `1` cell
  can only show a 1 and is drawn half a cell narrower than the others.
- **A CW slot too shallow.** Its height, not its width, is what sets the dot
  pitch and therefore legibility — confirmed by rendering at 720p, where the
  matrix came out at a 3-pixel pitch because the slot is short. The character
  count is deliberately *not* specified as an exact number: `HUD_TICKER_CHARS`
  is ours to set once the slot has been measured, so the artwork only has to
  provide a well-proportioned space. This is the general rule — pin down what
  cannot be changed afterwards, leave free whatever the code can adapt to.
- **Panels 7, 8 or 9 running full height.** They are a two-row block: 7 and 8
  side by side above, 9 spanning both below. A full-height PWR panel collides
  with the CW slot.

The v1 prompt asked for dummy values everywhere and got them rendered cleanly,
so the generator has no trouble filling recesses — which is exactly why the
"must not appear" summary is spelled out at the end of the prompt.
