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

## What this needs decided

1. **What it displaces.** Splitting the right column (scope above, map below,
   672x~350 each) is the cheapest; shrinking the cast is the other option —
   both terminal panes are more than half black for most of a round.
2. **Aerial or outline.** Outline needs no licence, no fetch and reads at size.
   Aerial needs a source decision and only means anything at a scale where no
   worked station fits.
3. **True range or spread range**, per above.
