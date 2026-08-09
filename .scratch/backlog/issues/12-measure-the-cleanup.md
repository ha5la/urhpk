# 12 — Measure the code, and track it across the cleanup

Status: ready-for-agent

Pick a metric and watch it while the structural cleanup runs. Candidates: raw
line count, cyclomatic complexity, or something like maintainability index.
Tooling exists — `radon` is the usual answer for complexity and MI in Python,
and `ruff` already computes complexity for its C901 rule, so a metric may be
available without a new dependency.

The cleanup has already started, so the measurement has to be **retroactive**:
walk the meaningful points of git history, measure each, and present the
series — a graph or a table.

## Notes

- Reasonable commits to sample so far, oldest first: `742b28a` (before this
  effort), `ebb715a` (scope_preview dropped), `720028f` (uv project),
  `0666387` (geo.py), `8176166` (wiring.py). More will follow.
- Line count alone will mislead here. Two of this effort's commits *add* lines
  on purpose — `tests/test_geo.py` is 116 lines of new tests, and a comment
  turned into a shorter one still leaves a line. Complexity and duplication
  are the honest measures of what is actually being compressed; count lines of
  *code* separately from lines of *test*.
- Watch for the metric becoming the goal. The spec's standard is "nothing more
  can be removed and it still implements the spec", which no counter measures.
