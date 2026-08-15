# 08 — Animate the HUD chips like lamps

Status: resolved

Give the HUD's chips lamp-like behaviour rather than instant on/off.

Constraint from the existing HUD work: `draw_hud_frame` is called ~24,000
times for a 2 h round and is kept cheap by `hud_frame_key`, which redraws only
when something visible changed. An animation that changes every frame defeats
that cache — the key would need to include the animation phase, and the cost
of that should be measured, not assumed.

"No visual glitches" applies: a lamp that flickers for one frame during a
state transition is a bug, not a detail.

## Answer

Asymmetric ramp, `HUD_CHIP_RISE_S` 0.08 / `HUD_CHIP_DECAY_S` 0.35 — the
asymmetry a filament actually has. Only the transitions animate.

**The cache worry was aimed at the wrong shape.** A *steady* glow would indeed
cost the whole reuse; a *transition* ramp is nearly free. Measured on the
August round by counting distinct `hud_frame_key`s across its own timeline:
118 transitions add at most ~1,000 draws to the 26,557 the real render
performed for 218,995 frames. `score_flash` was already the precedent — a
transient float quantised into the key.

**The lit pair now comes from telemetry**, not `SegState`. Since `icom_net`
the radio pushes freq/mode the instant either changes, so telemetry is the
source that knows, and it reports across the stretches where nothing was being
recorded. `hud_chip_marks` is the same split, for the same reason, as the
compass and `hud_az_marks`.

That turned up a real gap: `load_telemetry` had `az_offline` and
`meters_offline` but nothing for the rig, so the null pair the logger writes
when the radio drops was indistinguishable from a line silent about it. Fixed
as its own commit; every round on disk carries such lines.

**No glitches, checked against reality**: every frame of the August round
sampled at 25 fps — no chip reverses direction within a frame, none is left
part-lit for a single frame, and no two transitions land closer than 1.000 s.
The interruption case (a chip switched off and straight back on resuming from
where it faded to) has no instance in that data and is covered by a test
instead.
