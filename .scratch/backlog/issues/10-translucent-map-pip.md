# 10 — Translucent map beside the cast PiP

Status: needs-info

There is plenty of unused space next to the cast PiP. The vision: a
third-person aerial view of the QTH that **rotates with the antenna**, with a
pin dropped for each station worked — Google Earth-like.

Feasibility is the first question, and it splits into three: where the imagery
comes from and whether it can be embedded rather than fetched at render time;
whether rotating it per frame fits the render budget; and whether it reads as
anything at PiP size.

The data is already there — the rotator azimuth is a time series
(`hud_az_marks`) and every QSO carries a locator, hence a bearing and a
distance. Note the distinction: the map rotates with the **azimuth**, and the
pins sit at each station's **bearing**.

## Comments

**Feasibility, measured 2026-09-21.** Mocked at the real panel size against the
September round's own 11 worked stations (33–233 km, bearings 30–316° with five
of them inside one degree of 307°).

**The premise is gone: there is no unused space next to the cast PiP any
more.** Ticket 32 gave that space to the scope panel. At 1080p the row above
the HUD is now cast `1188x708` at x=20 and scope `672x708` at x=1228, with a
20 px margin either side and nothing between them. A map has to take room from
something, and that is the decision this ticket now turns on.

**The rotation is free, which was the question the ticket feared most.**
Rotating per frame is indeed fatal — 36.5 ms for a 512 px image, 153 minutes of
CPU across a 2 h 20 m round; 1024 px costs 459 minutes. But the azimuth is a
step function with very few steps: the September round wrote 1,238 azimuth
samples over **206 distinct whole degrees**. Cached by whole degree the whole
round's rotations cost **13 s** at 512 px, 39 s at 1024. Nothing here needs to
be clever; it needs a dict.

**The aerial view and the pins cannot share one picture.** A panel that holds
the worked stations is showing ~256 km of radius in ~350 px, which is
**1.46 km per pixel — OSM zoom 6**. A "Google Earth-like" view of the QTH is
zoom 17–19. That is eleven zoom levels, a factor of about 2,000; at z6 aerial
imagery is an undifferentiated green-brown field with no feature the operator
would recognise, and at z17 not one station worked in this round is inside the
frame. Whichever half is kept, the other is not a matter of tuning.

**What does read at that size is country outlines.** Natural Earth 1:50m
admin-0 (public domain, ~1.6 MB, embeddable beside the HUD artwork the same
way — no fetch at render time, no tile-server policy, no attribution overlay)
drawn as 1 px coastline and border lines. Hungary and its neighbours are
recognisable at 350 px and stay recognisable rotated. `/tmp/ticket10/` has the
mocks; the shapefile is a plain struct parse, ~25 lines, no geo dependency.

**The one real trade left is range compression.** Linear range keeps the map
geographically true and piles eight of eleven stations into the inner fifth of
the radius. `sqrt` spreads them beautifully and visibly warps the country
outlines — which costs exactly the thing the basemap was added for. Linear plus
a smaller span, with anything beyond it clamped to the rim, looks like the
honest compromise; colliding pins (five stations at 307°/47 km here) still need
dodging or a count badge either way.

## Decided

**Where it goes**: the scope panel splits horizontally — waterfall above, map
below, roughly `672x350` each at 1080p.

**Not a top-down map.** The view wanted is a drone shot above the QTH, turning
to face the station being worked — Google Earth, not a chart. The outline mock
above is not it.

## The drone view, measured

Feasible, and cheaper than the top-down one.

**No 3D engine and no Google Earth Studio needed.** A tilted pinhole camera
looking at flat ground is exactly a homography of an ortho image, and JN97TF
sits on the Pest plain; at any altitude worth using, relief is below a pixel.
So it is *one* aerial image plus a per-pixel inverse map. `/tmp/ticket10/`
has the geometry working against a synthetic ground texture — the imagery is
the only thing still to source.

**Cost: 34 ms per heading, 12.4 s for all 360.** Same cache-by-whole-degree
argument as the top-down view, so the render-time cost is a dict lookup.
Storage is one image, not a tile set: at 25 km altitude the view spans ~200 km
of ground in 672 px, so ~300 m/px is already oversampled and a 2048² picture
covers 600 km. A low-altitude view would need its own, much sharper image over
a few km — two altitudes means two images, not a pyramid.

**It cannot carry the pins, at any altitude.** Two independent reasons, both
visible in the mock:

- **A perspective view is a wedge.** At 60° horizontal FOV it shows ±30° of
  bearing, so with the beam at 307° this round's stations at 251°, 271°, 169°,
  121° and 30° are simply off-screen. The top-down plot shows all eleven at
  once; this shows two or three.
- **Distance collapses onto the horizon.** A station at 47 km and one at
  233 km land within a few pixels of each other vertically, at every altitude
  tried (0.3, 3, 25 and 90 km). Perspective spends its resolution on the
  nearby ground, which is the part with nothing in it.

So this is an atmosphere element: it says where the antenna is pointing,
beautifully, and says nothing about who was worked. That is a legitimate thing
to want — but it does not replace the worked-station display, and the two
cannot be the same panel.

## Still to decide

1. **Imagery source and licence** for the one ortho image. Open options
   (Sentinel-2 is 10 m/px and free) are more than sharp enough at 25 km
   altitude; a low-altitude view is where a licence question actually bites.
2. **Altitude and tilt** — 25 km / 35° is the one in the mock that reads best.
3. **What drives the heading**: the rotator azimuth (already a telemetry time
   series, ground truth for where the antenna points) or the bearing of the
   station being worked (from the input log's `qso` events). They normally
   agree; the rotator is the simpler and more honest of the two.
4. **Whether the pins get their own home** — this panel cannot hold them.

## The path shot, on real imagery

`/tmp/ticket10/path_view.py`, rendered against Esri World Imagery tiles for
four of the September round's own paths (33, 47, 168 and 233 km).

**Framing both ends composes itself.** The camera sits behind the QTH along
the bearing at `back = 0.45 D`, `alt = 0.38 D`, tilted to the mean of the two
depression angles. That puts the QTH in the lower third and the worked station
in the upper third for *any* path length, with no per-station tuning. The whole
camera is three numbers derived from the bearing and the distance.

**Flat ground is enough, confirmed against real imagery.** The ground is a
per-pixel inverse map of one ortho mosaic; no DEM, no 3D, no Earth Studio. It
looks like Google Earth because the imagery is the same kind of imagery.

**Tiles are bounded by picking the zoom from the distance.** The mosaic is
sized to a fixed ~2048 px budget, so the coarsest zoom that fills it is used:
z10 at 104 m/px for a 47 km path, z7 at 830 m/px for 233 km. That is 64–100
tiles per zoom level, fetched once per QTH and cached — not per round and not
per frame. Without it a 233 km path asked for about 1,100 tiles, which is not a
thing to do to a public tile server.

**Caching is now per worked station, not per degree.** The camera depends on
bearing *and* distance, so a round costs one render per unique station — 11
this round, ~34 ms each. If the view swings between stations rather than
cutting, that is ~30 interpolated frames per QSO, about 40 s of CPU for the
whole round. Either way the render budget is a non-issue.

### Composition: settled

**Proportional pull-back** (`back = 0.45 D`, `alt = 0.38 D`). Every path
composes identically and the caption carries the distance. `D^0.55` was tried
and rejected: it makes a long path read as long, and pushes the far station off
the top edge at 168 and 233 km.

### What drives it: settled

**The station being worked, not the rotator azimuth.** The HUD's compass
already shows where the antenna points, and saying it twice earns nothing.

### Imagery licence, before this ships

The mock uses Esri World Imagery, which is free to *use* with attribution but
is not obviously clear for a published video. **Sentinel-2 (Copernicus) is the
safe one**: open licence including commercial use, and its 10 m/px is far
sharper than any of these views need — the 47 km shot is already oversampled at
104 m/px. Worth switching before anything is rendered for YouTube.

## The swing between stations

`/tmp/ticket10/swing.py` → `swing.mp4`: one second on HG5O (47 km, 307°), a
1.4 s move, one second on HA3KHB (168 km, 251°). 102 frames at 672x350.

- **Smoothstep on the transition parameter**, bearing taken the short way
  round. Distance is interpolated in *log* space, so the pull-back — from a
  47 km framing to a 168 km one — feels even rather than rushing at one end.
- **Both stations are drawn at their true positions throughout**, the old one
  fading out over the first half and the new one in over the second. Nothing is
  ever shown at a place no station is: mid-swing the camera points at a bearing
  nobody was worked on, and the pins are simply where they really are.
- **The QTH stays pinned at frame centre** by construction — the camera always
  sits directly behind it along the current heading, so `across` is identically
  zero. Verified numerically across the swing rather than by eye.
- A station still above the top edge **waits until it is in frame** before its
  label is drawn; clamping the label instead put it on top of the caption.
- **51 ms per frame** at this mosaic size. A round of 41 QSOs, swinging for
  1.4 s at each, is **88 s** of render — plus one still per unique station.

**One production caveat the demo shows.** It uses a single mosaic sized for the
longer of the two paths, so the HG5O end renders at 415 m/px where it got
104 m/px on its own, and is visibly softer. Production wants either a mosaic
per segment or a two-level texture — coarse and wide underneath, fine and
narrow near the camera.
