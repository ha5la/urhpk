# 08 — Purge

Status: needs-triage

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
