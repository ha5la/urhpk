# 08 — Purge

Status: resolved

Two classes, both requiring evidence:

- **Provably unreferenced**: symbols nothing calls, imports nothing uses, flags
  nothing passes. Grep and the AST settle these.
- **Reachable only via inputs that can no longer occur**: superseded fallbacks,
  formats no longer written. Each one is argued individually before removal —
  the argument is what makes it safe, and it belongs in the commit message.

**Test coverage is not evidence.** `puskas_logger.py` sits at 40% because its
UI is verified by running rounds. Deleting on coverage would delete the logger.

Recent history shows this seam is productive: two superseded fallbacks and
`--no-hud` were already removed this way.

## Answer

Small, which is the finding. An AST scan over every top-level name in every
module, cross-referenced against production and test files, plus a scan of
class methods, dataclass fields and CLI flags, turned up one genuinely dead
symbol out of 439.

**Removed: `icom_net.parse_token_response`.** Added with the module and never
called. Not merely unused, either: the register step does not check the token
reply at all — it waits for the capabilities packet the radio pushes
afterwards, which is stronger evidence that the token was accepted than the
reply's own status word.

**Argued and kept**, each for a different reason, which is why the scan's
output is not the answer on its own:

- `CallsignCompleter.get_completions` and `_CastScreen.reverse_index` have no
  caller in this repo because their callers are prompt_toolkit and pyte.
- `icom_net.bcd_encode_freq` has no production caller because nothing here
  ever sets the radio's frequency. It is how the integration tests' mock radio
  *speaks* the protocol; deleting it would mean hand-written BCD literals in
  the double, which is strictly worse.
- `hud.swr_ratio` / `hud.po_percent` and their curve tables convert two
  recorded meters that nothing displays — the HUD artwork has `vd` and `id`
  recesses and no SWR or Po slot. Kept on the operator's call: the raw values
  are recorded and loaded end-to-end already, and FINDINGS.md names the
  concrete next use (the radio switches its S-meter slot to Po on transmit, so
  a TX-time Po readout is a slot away, not a measurement campaign away).

**No candidates in the second class at all.** Nothing was reachable only via
inputs that can no longer occur. The two obvious suspects both survive on
evidence rather than on caution: `load_telemetry` accepts whole-second stamps
because recordings written by the original 1 Hz sampler still exist, and
`FREQ_MATCH_TOLERANCE_HZ` exists for the same old recordings, whose systematic
sub-kHz disagreement was our own rounding. Both are documented as such in
ARCHITECTURE.md. `--no-hud` and the two superseded fallbacks the ticket
remembered were already gone before this pass.

The reason the yield is this low is worth writing down: the seam has been
productive *because* it was worked repeatedly, not because there is a standing
reservoir of dead code. Issues 01-06 removed far more than this pass did, by
removing whole files and whole duplicate definitions rather than orphans.
