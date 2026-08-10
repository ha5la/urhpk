# 14 — README is 42% terminal setup, and the root has eight docs

Status: resolved

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

## Answer

The sketch, with one correction. `docs/` now holds PIPELINE, ARCHITECTURE,
RECORDING, FINDINGS and the new `operator-setup.md`; README, CLAUDE and
CONTEXT stayed at the root.

The correction is the artwork prompt, which the sketch sent to `docs/` and
which belongs in `hud-theme/artwork-prompt.md` instead. It is not narrative
documentation about the project — it is the *source* `artwork.png` was
generated from, and the directory already keeps provenance for its own
contents (`DSEG7Classic-Bold.LICENSE` beside the font). `--hud-theme DIR`
means a theme can be copied wholesale, and the instructions for regenerating
its artwork should travel with it. `load_hud_theme` opens named files, so the
extra markdown file is inert. Renamed on the way in: the `hud-` prefix was
only earning its keep while the file sat at the root.

Two things worth knowing for the next such move:

- **Nothing broke, because nearly every cross-reference is a bare filename in
  prose, not a link.** Only README.md linked with paths. So the rule is now
  written down in CLAUDE.md's document table: the docs are referred to by bare
  filename, and moving one is a `git mv` plus the table.
- **`docs/agents/domain.md` names three of them by path** and had to be
  updated. It is the one file outside the docs that cares where they live.

The README's 50 lines of irssi/tmux/bell settings became
`docs/operator-setup.md` and are replaced by a two-line pointer, which is what
the ticket was really about — the front page is now the components table, two
quick starts, testing, and the documentation index.
