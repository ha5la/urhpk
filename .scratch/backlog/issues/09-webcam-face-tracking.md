# 09 — Face tracking instead of fixed centering

Status: needs-triage

The webcam PiP is centered on a fixed crop. Track the face instead.

Decide before implementing: the tracker's cost across a 2 h clip, what happens
when the face leaves frame entirely (hold? drift back to centre? both look bad
in different ways), and how much smoothing is needed so the PiP does not
jitter. The clip is rendered once, offline, so an expensive tracker is
affordable in a way a live one would not be.
