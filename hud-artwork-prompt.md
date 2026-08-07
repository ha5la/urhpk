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
| Score, QSOs, QRG, volts, amps, bearing | DSEG7 segment font |
| UTC / rate / ODX rows | DSEG14 segment font |
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

TOP REGION — the HUD bar. A very wide letterbox strip, exactly 7.4 times wider
than it is tall, spanning the full width of the image. It contains these
panels in one horizontal row, left to right:

1. Wide panel, empty dark recess, small label "SCORE" centred at its bottom.
2. Narrower panel, empty dark recess, label "QSOS" at its bottom.
3. Panel with an empty dark recess at the top and the small label "MHz" under
   it; below that, two rows of small rectangular selector chips reading
   [2M][70CM][23CM] and [SSB][CW][FM]. Draw ALL SIX chips brightly lit in
   amber — none dark.
4. Narrow panel: an empty circular recess in the upper half, and below it an
   empty horizontal slot for a bar meter, with the small label "S" beneath.
5. A tall portrait rectangular recess with a heavy bevelled frame, its inside
   flat dark grey and completely empty. This is where a webcam image will go.
6. Panel with a circular compass rose showing only the letters N, E, S and W
   around its edge and NO needle or pointer of any kind. Below the circle,
   empty space, then the small label "ROT".
7. Narrow panel with two empty value slots stacked vertically, each with a
   small label to its right — "V" for the upper, "A" for the lower — and the
   label "PWR" at the bottom.
8. Panel, only about half the height of the bar, sitting in the upper half,
   containing three rows. Each row has a small grey caption on the left and
   empty dark space on the right. The captions are "UTC", "RATE /H" and
   "ODX KM".
9. Directly below panel 8, filling the bar's lower half and the same combined
   width as panels 7 and 8: a wide empty dark letterbox slot with the small
   label "CW" beneath it. This slot is a dot-matrix display whose dots are
   drawn by software, so leave its interior completely flat and empty — no dot
   grid, no pixel matrix, no characters, no texture of any kind inside it.

CRITICAL: the HUD bar must contain NO numbers and NO digits anywhere. Every
value area is an empty dark recess. Only the labels listed above appear.

BOTTOM REGION — five separate pieces on a flat solid magenta (#FF00FF)
background, evenly spaced in one row, in exactly this order, each surrounded
by a generous margin of plain magenta so it can be cut out cleanly:

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
```

## Known failure to watch for

The first generation came back at 4.4:1 against a requested 8.7:1. The bar's
aspect ratio is the one thing worth rejecting and re-rolling for: the height
budget is what forced the whole ticker/PWR/stats restructure, and a bar taller
than about 260px at 1920 wide collides with the terminal PiP above it.
