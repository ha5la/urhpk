# 08 — Animate the HUD chips like lamps

Status: needs-triage

Give the HUD's chips lamp-like behaviour rather than instant on/off.

Constraint from the existing HUD work: `draw_hud_frame` is called ~24,000
times for a 2 h round and is kept cheap by `hud_frame_key`, which redraws only
when something visible changed. An animation that changes every frame defeats
that cache — the key would need to include the animation phase, and the cost
of that should be measured, not assumed.

"No visual glitches" applies: a lamp that flickers for one frame during a
state transition is a bug, not a detail.
