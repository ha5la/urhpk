# 05 — Split contest_video.py (3,995 lines)

Status: needs-triage

Blocked by: 03

Too big to hold in one head, independent of any duplication. Candidate seams,
in rough order of how cleanly they cut:

- CW decode (pitch detection, envelope, gating, trust gate)
- audio/timeline sync (offsets, anchors, drift fitting)
- the HUD (theme, recesses, needle, meters, ticker)
- the cast PiP renderer (pyte replay to frames)
- the scope waterfall
- ffmpeg filtergraph assembly

The constraint an edit must not break: how filter branches *combine* is not
covered by unit tests, so each extraction needs a real render checked against
a decoded frame, not just a green suite.
