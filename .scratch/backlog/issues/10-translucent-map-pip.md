# 10 — Translucent map beside the cast PiP

Status: resolved

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
104 m/px on its own, and is visibly softer. Two levels fix it — see below.

## Two texture levels, and the imagery swap

Both settled together, because the levels are what made the swap worth doing.

**The blur was never about the source, it was about one texel size having to
serve the whole frame.** A single mosaic must reach the far end of the path,
and that sets its resolution everywhere — including the near ground at the
bottom of the frame, which is exactly where a tilted camera has the most
resolution to spend. Two levels, sampled coarsest-first so the finer one paints
over where it reaches: **z8 at 415 m/px** covering the 168 km path, and
**z11 at 52 m/px** covering ±50 km of near ground. `/tmp/ticket10/twolevel.png`
is the same HG5O frame rendered both ways; the one-level version is mush where
the two-level one has Budapest's streets and the Danube.

**Sentinel-2 supports it, and suits it better than Esri did.** EOX's
`s2cloudless` is a standard XYZ/WMTS pyramid — z8 and z11 both fetch normally —
so the level machinery is source-agnostic. What Sentinel-2 adds:

- **Both levels are decimations, never upsampling.** 10 m/px native is ~z13.4
  here, so even the fine level at z11/52 m/px is still 5x coarser than the
  source. The pyramid does run out just past z13; nothing here goes near it.
- **It is a cloud-free annual composite**, so colour is consistent between the
  two levels. Esri's mosaic of varying acquisition dates would put a seam at
  the level boundary on top of the resolution one.
- **CC BY 4.0**, which is the point of the swap — Esri's terms were never
  clearly good for a published video.

**Cost.** 66 ms/frame against 49 for one level: one extra gather over the same
pixel grid. A 41-QSO round's transitions go from 88 s to ~114 s. Tiles are 202
for this QTH at these two levels, fetched once and cached — not per round.

**The level boundary turned out not to need anything.** Sentinel-2's composite
keeps the colour identical across it, so the only discontinuity is sharpness,
and the haze at that range covers it. Bilinear sampling (below) softens what
was left. No cross-fade band was needed.

## The swing, rendered the settled way

`/tmp/ticket10/swing2.py` → `swing2.mp4`. Same 1 s / 1.4 s / 1 s shape as
before, now on Sentinel-2 with both levels and **bilinear** texel sampling.
Sharp from the QTH to the horizon at both ends of the move, and nothing about
the level boundary is visible.

**Bilinear is what the rest of the quality costs.** 248 ms/frame against 66 for
nearest — 3.8x, because each level becomes four gathers over the whole pixel
grid instead of one. A 41-QSO round's transitions go to about 7 minutes. That
is still small against the rest of a render, and it is unoptimised: the fine
level covers a minority of the frame, so gathering only in-bounds pixels is the
obvious saving and has not been tried.

So the full recipe, all of it measured rather than guessed:

| Decision | Settled on |
|---|---|
| Where | lower half of the scope panel, ~672x350 |
| Driven by | the station being worked, not the rotator azimuth |
| Camera | `back = 0.45 D`, `alt = 0.38 D`, tilt = mean of the two depressions |
| Move | smoothstep; bearing the short way, distance in log space |
| Ground | two levels, z8 at 415 m/px + z11 at 52 m/px, coarsest painted first |
| Sampling | anisotropic, 4 taps (see below) |
| Imagery | Sentinel-2 cloudless (EOX), CC BY 4.0, ~200 tiles once per QTH |
| Cost | 757 ms/frame in the full renderer, ~22 min for a 41-QSO round's transitions |

## Built

`urhpk/qth_map.py`, composited by `contest_video.py` into the lower half of the
right-hand column. ARCHITECTURE.md has the component notes; what the prototypes
settled is all above.

What integration added beyond the prototypes:

- **The right column splits** the way the row above the HUD already did, with
  both rects fixed before either branch is built. Both panel heights are forced
  even — each is encoded as its own h264 clip, and x264 refuses an odd
  dimension, the same trap `hud_height` fell into.
- **The map panel is opaque**, unlike the cast PiP beside it. It is a panel of
  its own rather than an inset, and there is nothing behind it worth seeing.
- **Frame reuse**, via `Shot.key()`, the same mechanism as `hud_frame_key`. The
  camera only moves between QSOs, so a round is a few thousand distinct renders
  rather than a quarter of a million.
- **Never fatal.** The imagery comes off a public tile server. A round that
  cannot reach one, or that logged no locators, prints one line saying so and
  renders without the map — the waterfall keeps the whole column, exactly as
  before this existed.
- **Tiles cache in `~/.puskas/tiles/`**, not the round directory: the imagery
  belongs to the QTH and every round from here wants the same tiles.

## The shimmer, and why bilinear could not fix it

Reported as pixel flicker while the animation runs. It is minification
aliasing: a screen pixel's footprint on the ground grows with range and is
stretched along the view direction, so in the distance it covers several texels
vertically and about one horizontally. Bilinear averages 2x2 texels whatever
the footprint is, misses most of it, and lands on different texels as the
camera moves — which is the flicker.

Two properties of a ground plane under a camera with no roll make the fix
cheap: **the footprint depends only on the screen row**, and **its long axis is
screen-vertical**. So the mip level and the tap count are per-row constants,
the taps are sub-pixel offsets in y, and the rows sharing a mip level are a
contiguous band — slices, not masks.

Measured against a 128-tap reference, on the HG5O frame:

| | ms/frame | RMSE vs reference |
|---|---|---|
| bilinear (what the earlier videos used) | 244 | 7.41 |
| mip only | 220 | 6.53 |
| anisotropic, 4 taps | 573 | 0.53 |
| anisotropic, 8 taps | 1069 | 0.16 |
| anisotropic, 16 taps | 1935 | 0.10 |

**Mip alone barely helps** — which is the whole reason the answer is
*anisotropic* and not just mipmapping. Choosing the level by the long axis
blurs the ground to mush; by the short axis it still aliases. Only spreading
taps along the long axis fixes it.

**Four taps is enough here, and that is a consequence of the composition.**
The footprint anisotropy measured 3x median and 6x maximum, not the tens a
grazing camera would give — because the proportional pull-back keeps the tilt
at about 27°, not near-horizontal. 8 taps is visually identical to 4.

Two mistakes worth not repeating, both caught by looking at the render rather
than the numbers:

- The taps must spread across **one screen pixel** in y. Spreading them by the
  anisotropy *ratio* in pixels smears the ground into mush — and did, until the
  crop showed it.
- That same error was in the reference frame the RMSE was measured against, so
  the first table made bilinear look four times worse than it is. A reference
  has to be the same thing done properly, not the same mistake done harder.

Where the taps cannot cover the long axis (not the case at this tilt, but it
would be at a lower one), the level is raised to what they *can* cover — the
standard bounded-anisotropy formulation, so cost stays fixed as tilt drops.

## Terrain, and what looking for it found instead

Reopened by a question: the panel renders imagery onto a plane, so what would
a DEM under it buy? The answer is nothing, but finding that out turned up the
term the flat model should have had all along.

### Terrain: built, measured, rejected

`/tmp/ticket10/terrain.py`. DEM is Mapzen terrarium off AWS Open Data, the
same XYZ scheme as the imagery, so the level machinery is unchanged.

**A heightfield has no inverse map.** Flat ground is a homography and the
renderer inverts it per pixel; a ray over terrain can clear one ridge and be
stopped by the next, so this marches every ray outward — 420 geometric steps,
linear interpolation at the crossing, live-pixel compaction. That is the whole
cost difference, and it is structural rather than a tuning failure.

**At the settled composition relief is a rounding error.** Vertical
displacement of the ground, in screen pixels:

| path | peak in frame | median | 99th |
|---|---|---|---|
| HA7NS 37 km | 270 m | 2.0 px | 3.7 px |
| HA3KHB 168 km | 1835 m | 0.5 px | 1.5 px |
| HA1VHF 233 km | 2114 m | 0.4 px | 1.4 px |

**It gets worse with distance, which is the composition's own doing.**
`alt = 0.38 D` scales with the path and mountains do not, so the longer the
path the smaller the relief. The pull-back that made every path frame itself
is exactly what makes terrain invisible.

Nothing rescues it at that composition: 8x vertical exaggeration and hillshade
are still indistinguishable, and dropping the camera from `0.38 D` to `0.06 D`
moves relief only 0.5 px to 1.1 px — the scene compresses toward the horizon
as fast as relief grows — while collapsing both pins onto the horizon.

The cause is angular size. 672 px over 60° is 0.09°/px; a 1 km hill at 80 km
subtends 0.7°. Google Earth's drama is the Alps at 20 km, not the Pest plain
at 170.

### Earth curvature is the term that was actually missing

Same metric, same panels — and it scales up exactly where terrain scales down:

| path | terrain | curvature | drop at the DX |
|---|---|---|---|
| HA7NS 37 km | 2.0 px | 1.1 px | 226 m |
| HA3KHB 168 km | 0.5 px | 5.2 px (99th 22.5) | 4657 m |
| HA1VHF 233 km | 0.4 px | 7.3 px (99th 36.3) | 8958 m |

On the long paths it is 10–18x what terrain does. Rendering a DEM on a flat
earth would have modelled the small term and ignored the large one.

**It needs no march.** The ray meets `z = -d²/2R` in closed form: a quadratic
in the horizontal distance instead of a divide, and its discriminant going
negative *is* the horizon. Measured in `draw_ground` at 672x350, 4 taps, two
levels: `_ground` alone 1.24 ms flat against 8.19 ms curved, but it is twelve
calls in a frame dominated by bilinear gathers, so the frame goes **550 ms to
633 ms — +83 ms, +15%**. Whole frames on real tiles measured 517–655 ms flat
against 589–644 ms curved. About +3 min on a 41-QSO round's transitions.

The visible change is the far third: the ground falls away, more of it fits,
and the DX pin drops from floating above the ground onto it — on a flat earth
the renderer puts a 233 km station 9 km too high.

### The horizon has to be framed for, and the mosaic has to reach it

At the shipped camera the horizon is 1062 km away and the mosaic stops at
443 km, so it is never in shot. (Worth recording: an earlier read of these
renders called a curved edge "the horizon". It was the mosaic boundary bent by
curvature.)

Framing it is two constraints — QTH on the 90% row, horizon on the 10% row —
in three unknowns, so altitude stays free, and altitude is what decides where
the DX lands, because the horizon runs away as `sqrt(2·alt·R)` while the
station stays at 233 km:

| alt km | back km | tilt | horizon from QTH | HA1VHF row | near edge | m/px bottom |
|---|---|---|---|---|---|---|
| 10 | 17 | 16.7° | 340 km | 10.6% | 15 km | 26 |
| 20 | 33 | 18.1° | 472 km | 12.9% | 29 km | 50 |
| 45 | 68 | 20.3° | 690 km | 19.3% | 60 km | 103 |
| 70 | 99 | 22.0° | 845 km | 24.7% | 88 km | 151 |
| 110 | 145 | 24.1° | 1039 km | 31.7% | 129 km | 222 |

Two things fall out. **A low camera crowds the pins** — at 10 km the station
sits 70% of the way to the horizon, which is why they collide. And **a low
camera is what the blurry foreground was**: 26 m/px at the bottom row against
a 52 m/px fine level is 2x magnification. Both clear at about 45–70 km.

Keeping `BACK_FRAC`/`ALT_FRAC` and moving only the tilt does not work: it puts
the QTH at 102%, just off the bottom. The fractions would go to roughly
`alt = 0.30 D`, `back = 0.43 D`.

Showing a horizon also needs a **third texture level** — z6 at 1.66 km/px — for
the band between the existing wide level and the horizon. 49 tiles, and the
renderer already paints coarsest-first, so no code change.

### Why terrain still fails once the horizon is visible

This is the case terrain should have won, and the reason it does not is not
geometry.

**At 70 km altitude the geometric horizon is 845 km from the QTH, and no
amount of atmosphere lets you see that far.** The longest documented sight
line on Earth is around 440 km. At a measured haze curve the horizon sits at
92% opacity, so everything terrain does out there is behind grey.

Drop to 20 km — horizon 472 km — and cheat the haze the way Google Earth does,
and it finally appears: the horizon is visibly serrated and the snow on the
High Tatras reads as a bright ridge.

| feature | from QTH | height | silhouette |
|---|---|---|---|
| Mátra | 80 km | 1014 m | 5.2 px |
| Low Tatras | 200 km | 2043 m | 5.3 px |
| High Tatras | 250 km | 2655 m | 5.7 px |
| E Carpathians | 500 km | 2061 m | 2.4 px |

An order of magnitude better than 0.4 px — and still invisible at panel size.
It shows in a 3x crop, and a 3x crop is not what ships. The march costs
**51–76 s/frame** supersampled, about 100x the current frame.

**Hillshade does not save it, and cannot.** It is the cheap kind of shading —
Lambertian off the DEM gradient, no ray casting, 0.6–0.8 s a frame — and it
changes the picture a great deal (27 grey levels mean, 60 at the 99th) without
ever reading as landscape: at sun 8° the frame goes muddy, at 45° it is barely
there. Sentinel-2 cloudless is an annual composite with its own near-nadir
illumination already in it, so a second light is two contradictory suns on one
surface. Cast shadows were not built: if hillshade cannot read, they cannot.

**So terrain needs three things to go right at once** — a camera lower than the
pin spacing wants, a haze model nobody believes, and a light that fights the
imagery — for a payoff that vanishes at 672x350. Dropped.

### Decided, not built

Three changes, all independent of the DEM, so dropping terrain costs none of
them:

- **Curvature** — `z = -d²/2R`, closed form, in `_ground` and `project`.
  **+15%**, and it is the only one of the three that stands alone.
- **Supersampling** — the horizon is a hard per-pixel boolean, so it
  staircases; 3x removes it completely. **0.2 s to 1.6 s**, or far less if only
  the ~40 rows around the horizon are supersampled.
- **Borders** — Natural Earth 50m admin-0, the struct parse from the old
  basemap mock, projected through the same camera and hidden past the horizon.
  Too cheap to separate from the supersampled frame it was measured in.

Curvature is worth doing on its own merits — it is the 5–7 px error, and it
puts the DX pin on the ground — so it is cut out as **ticket 33**.
Supersampling and borders only pay for themselves if the camera is reframed so
the horizon is in shot, which is a composition change this ticket has not
taken, and they stay here until it is.

Mocks: `terrain.py`, `curve_bench.py`, `curve_px.py`, `curve_look.py`,
`reframe.py`, `skyline.py`, `haze_test.py`, `shadow_test.py` in `/tmp/ticket10/`.

## Previewed against the shipped renderer

Two pairs — HA5LA→HA1VHF (233 km) and HA5LA→HG5O (47 km) — rendered by the
production renderer and by the same camera with curvature, 3x supersampling
and borders, before writing any of it. `/tmp/ticket10/preview_pairs.py` and
`preview_horizon.py`.

**At the camera as it ships, two of the three do nothing.** Curvature moves
the far ground and drops the pin onto it, and the borders are the largest
visible change — Slovakia and Austria read across the upper half of the 233 km
frame. Supersampling improves nothing anyone can point at, because its whole
justification is the staircased horizon and the horizon is 1062 km away while
the mosaic stops at 443. On the 47 km path none of the three is visible at
all: curvature is ~1 px there and no border falls inside 47 km of Budapest.
**0.61 s to 6.0 s a frame, about 10x, for one of the two paths.**

**Framed so the horizon is in shot, all three pay together.** At alt 45 km,
back 68 km, tilt 20.3°, horizon 690 km from the QTH on the 10% row: a hard
clean horizon with sky above it where the flat model merely fades into the
mosaic edge, borders running the whole way out to it, and at last the edge
supersampling exists for. **0.78 s to 8.3 s.**

**But that camera is three further changes, not one.**

- **It is the same camera for every path.** Pinning the QTH to 90% and the
  horizon to 10% uses both degrees of freedom, leaving nothing to adapt to
  path length. HA1VHF lands at 19% of frame height and HG5O at **55%**, barely
  above the QTH. The proportional pull-back — the rule this ticket settled on,
  and the reason a 33 km path and a 233 km one compose alike — is gone.
- **A third texture level is required**: z6 out to 1250 km, 49 tiles, for the
  band between the existing wide level at 443 km and the horizon at 690. The
  renderer already paints coarsest-first, so no code change.
- **The haze can no longer be scaled by the path.** `rng / (distance_km *
  3.2)` saturates at 150 km for a 47 km path; with 690 km of ground in frame
  the whole picture goes flat grey. The preview scales it by the horizon
  instead, identically in both panels so it does not confound the comparison.

So the horizon is not a feature that can be added to this panel — it is a
different panel. The 233 km frame is plainly better for it and the 47 km frame
is plainly worse than what ships today, and eight of this round's eleven QSOs
are under 82 km.

### Open

Which way the composition goes, which is what everything except curvature now
waits on:

1. the horizon camera for every path, losing per-path framing;
2. the shipped camera under some distance and the horizon camera over it,
   accepting that consecutive shots compose differently;
3. keep today's framing, take curvature and borders, drop supersampling.

Curvature is out as ticket 33 and is unaffected by this choice — it is a
correctness fix to ground already on screen. Supersampling and borders stay
here: borders are nearly free and read at either camera, supersampling only
earns its 10x once there is a horizon to anti-alias.
