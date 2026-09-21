# 31 — The second cast of a round looks opaque

Status: resolved

Both casts carry `colorchannelmixer=aa=0.85`, but the second one reads as solid
where the first reads as transparent.

## The measurement

Ten frames 0.4 s apart in each cast's stretch of the September render. The
waterfall scrolls, so whatever reaches through the PiP flickers; the terminal's
own text does not move enough to matter at the median.

| stretch | waterfall flicker, open canvas | flicker through the PiP | ratio |
|---|---|---|---|
| cast 1 @ 4200 s | 7.91 | 1.044 | 0.132 |
| cast 2 @ 4800 s | 4.59 | 0.000 | 0.000 |

0.132 is the 1 − 0.85 the alpha intends. 0.000 is the second cast blending over
the first's `tpad`-cloned last frame rather than over the waterfall: two
terminals at 0.85 leave the background 0.15 × 0.15 ≈ 2 % visible, and the layer
underneath looks like a terminal anyway, so the transparency stops reading.

## What to do

Each cast's overlay is enabled from its own start to the *next* cast's start,
`gte` only for the last. The `tpad` freeze still covers the gap between a cast
ending and the next beginning, which is the case it exists for.

## Verification

Two casts at 12.5 s and 4501.25 s: the first overlay reads
`enable='between(t,12.500,4501.250)'`, the second `enable='gte(t,4501.250)'`.

## Resolved

Fixed in the graph; the September round wants a re-render to carry it.

The webcam recess has the same shape and no such bug: those clips are opaque, so
the one underneath cannot show through. Left alone.
