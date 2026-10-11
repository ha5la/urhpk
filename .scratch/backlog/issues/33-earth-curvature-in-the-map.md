# 33 — The map panel draws a flat earth, and puts the DX pin 9 km too high

Status: ready-for-agent

`qth_map._ground` intersects the camera ray with `z = 0`. Over a 233 km path
the camera sits at 88.5 km and the ground falls 9 km below that plane by the
far end, so the whole far third of the panel is drawn in the wrong place and
the station marker floats above the ground it is standing on.

Measured in ticket 10, as the vertical displacement between where the flat
model draws a ground point and where it belongs:

| path | median | 99th | drop at the DX |
|---|---|---|---|
| HA7NS 37 km | 1.1 px | 4.1 px | 226 m |
| HA3KHB 168 km | 5.2 px | 22.5 px | 4657 m |
| HA1VHF 233 km | 7.3 px | 36.3 px | 8958 m |

For scale: the DEM that ticket 10 tested and rejected moved the same ground by
0.4 px on the same panel. Curvature is 10–18x the error terrain would have
fixed, and unlike terrain it costs almost nothing.

## What to change

**`_ground`** — the ray meets `z = -d²/2R` in closed form. With `slope = dz/rh`
and `rh = hypot(px, dy)`, the near root of the quadratic is

    d = R·(-slope - sqrt(slope² - 2·alt/R))

and `s = d / rh` replaces `s = -alt/dz`. The discriminant going negative is the
horizon, and takes over the role the `dz < 0` test plays today.

**`project`** — the same drop applies to the markers, or the pins keep floating
while the ground underneath them moves. A point at horizontal distance `d` sits
at `z = -d²/2R`, so `cam.alt_km` becomes `cam.alt_km - z` in both the depth and
the up term.

## Cost

Measured against the real `draw_ground`, 672x350, 4 taps, two levels:

| | flat | curved | |
|---|---|---|---|
| `_ground` alone | 1.24 ms | 8.19 ms | +560% |
| `draw_ground` | 550 ms | 633 ms | **+15%** |

`_ground` is twelve calls in a frame otherwise dominated by bilinear gathers,
which is why 6.6x on the part is 1.15x on the whole. Whole frames on real tiles
measured 517–655 ms flat against 589–644 ms curved. About +3 min on a 41-QSO
round's transitions, against the ~22 min they already cost.

## Scope

**Curvature only.** Ticket 10 also settled that a visible horizon would want
supersampling, country borders, a third texture level and a reframed camera —
all of which depend on a composition change this ticket does not make. At the
shipped camera the horizon is 1062 km away and the mosaic stops at 443 km, so
nothing here puts one in shot. Curvature stands alone because it is a
correctness fix to ground that is already on screen.

## Verification

- A unit test on the geometry, not the pixels: at 233 km the drop is
  `d²/2R = 8958 m`, and `project` must place the DX that far below where the
  flat model puts it. Pin the camera, assert the row.
- The discriminant test must reproduce the existing behaviour where no horizon
  is in frame — same covered mask as today at all eleven of the September
  round's paths, since none of them reach it.
- Render one frame at 233 km before and after and diff the far third; the
  expected shift is tens of pixels, not thousands.

## Previewed

Rendered against the production renderer on both ends of the round's range
(`/tmp/ticket10/preview_pairs.py`), at the shipped camera:

- **HA5LA→HA1VHF, 233 km** — the far ground drops and more of it fits, Balaton
  shifts visibly, and the pin lands on the ground instead of above it.
- **HA5LA→HG5O, 47 km** — indistinguishable. Curvature is about 1 px there.

That split is the expected one and is not an argument against the change: the
error it removes grows with path length, so the paths where it does nothing
are the paths that were already right.

**This ticket is independent of the composition question.** Ticket 10 is now
open on whether the camera should be reframed to put the horizon in shot,
which would change the framing rule, add a texture level and change the haze
model. Curvature needs none of that — it corrects ground that is already on
screen at the camera that already ships, and it should land whichever way
that decision goes.

Prototypes: `curve_bench.py`, `curve_px.py`, `curve_look.py`,
`preview_pairs.py` in `/tmp/ticket10/`.
Background and the rejected DEM: ticket 10.
