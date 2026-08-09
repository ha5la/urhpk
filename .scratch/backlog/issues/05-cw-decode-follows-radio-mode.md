# 05 — Decide CW decoding by radio mode, not by segment mode

Status: needs-triage

A WAV segment that started in another mode should still be CW-decoded if the
radio was switched to CW *during* it. Today the decision is made from the
segment's own recorded mode, so those are missed.

The machinery is largely present: `decode_long_segment`/`cw_subranges` already
find telemetry-confirmed CW sub-ranges inside a long segment. This is about
rebasing the *decision* on radio mode from telemetry rather than on the
segment's metadata.

Depends on `--telemetry`; without it there is nothing to rebase on.
