# 32 — The waterfall is behind the terminal, and it is the wrong third

Status: resolved

The scope is scaled to the full canvas and the terminal sits on top of it, so
the only visible waterfall is the strip to the right of the terminal — which is
the right-hand *edge* of the frequency span. The tuned frequency is at the
span's centre, permanently covered.

## What to do

Share the row above the HUD instead of stacking it: terminal on the left, scope
panel on the right, both rects fixed before either branch is built.
`SCOPE_PANEL_W_FRAC` (0.35) gives the panel its width and the terminal takes the
rest, `force_original_aspect_ratio=decrease` keeping its shape inside that box
whatever the round's terminal size. With no cast, the scope keeps the whole
canvas as before.

At 1080p: panel 672x708 at (1228,16), terminal bounded to 1188x708 — it was
1218x708, so it loses 30px of width and nothing else.

## Verification

Graph assertions for both rects, plus a rendered 720p preview decoded and
looked at: the span's centre trace is visible in the panel.

## Resolved

Layout in place. The September round still wants a re-render to carry it,
together with ticket 31's cast handover.
