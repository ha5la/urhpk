# 14 — README is 42% terminal setup, and the root has eight docs

Status: needs-info

Two problems, one ticket, because the fix for each is where the other lands.

**README carries one user's settings.** About 50 of its 119 lines are
"Getting notified of a private message" — irssi triggers, tmux
`monitor-activity`, SSH terminal bell forwarding. Useful knowledge, genuinely
worth keeping, but it is how *this* operator configured *their* terminal, not
what the project is. The front page should say what the components are and how
to run a round.

**The root has eight markdown files**: README, CLAUDE, CONTEXT, PIPELINE,
ARCHITECTURE, RECORDING, FINDINGS, hud-artwork-prompt. `docs/` already exists
for `docs/agents/`.

## Constraints on any move

- `README.md` stays at the root — GitHub renders it there.
- `CLAUDE.md` stays at the root — the harness reads it there.
- `CONTEXT.md` stays at the root — `docs/agents/domain.md` says so.
- The other five are free to move, and CLAUDE.md's document table plus every
  cross-reference between them moves with them.

## Sketch

`docs/` for PIPELINE, ARCHITECTURE, RECORDING, FINDINGS, hud-artwork-prompt,
plus a new `docs/operator-setup.md` for what comes out of the README. Three
files at the root, each with an obvious job.

Worth doing after the structure effort's file splits, not before — 05 and 06
will change what ARCHITECTURE.md has to describe.
