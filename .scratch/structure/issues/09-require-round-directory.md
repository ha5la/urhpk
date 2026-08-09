# 09 — Refuse to run in the project root

Status: resolved

A round produces a fistful of files — `*.edi`, `*-telemetry.jsonl`,
`*-input.jsonl`, `*.scope`, `*.cast`, `*-webcam.mp4`. One round's worth is
fine; several rounds' worth in one directory is a pile nobody can attribute.
CLAUDE.md already states the intent ("a contest directory holds exactly one
round"), but nothing enforces it, and the project root is exactly where a
mistaken launch lands.

Requirement: every component that **writes** round files refuses to start when
the current directory is the project root, and says what to do instead. A
round lives in its own subdirectory of the project.

## Notes

- The project root is now unambiguous: it is the directory holding
  `pyproject.toml`, which `uv` already walks up to find (issue 02).
- Affects the writers — `puskas_logger.py` and `contest_video.py` — not the
  read-only tools.
- `puskas_harvester.py` is a judgement call: it writes `.puskas_cache/` to the
  CWD but its real output goes to `~/.puskas/`, and it is run "days ahead",
  plausibly from the root. Probably exempt; decide before implementing.
- The guard runs at startup, before the radio and the recorders come up. It
  must be impossible for it to refuse a *legitimate* launch — a false positive
  here costs a contest round, which is worse than the pile it prevents.
- Needs a test with a pinned fake root rather than one that depends on where
  the suite happens to run.

## Answer

`wiring.round_directory_error(cwd, project_root)` decides;
`wiring.require_round_directory()` exits 2 with the message. The decision is
pure and takes both sides as arguments, so it is tested against a pinned root
rather than wherever the suite runs.

The root is found as `Path(__file__).parent`, not by walking up from the
current directory — the answer must not depend on the very thing being
checked. Both sides `.resolve()`, so reaching the root through a symlink is
still the root.

`puskas_harvester.py` is exempt, as the ticket expected: its real output goes
to `~/.puskas/` and it is run days ahead, plausibly from the root.

The false-positive rule bit twice, both times usefully:

- **`contest_video.py` is guarded only at the render.** `--hud-demo` and
  `--hud-theme-check` write one PNG and exit, and RECORDING.md tells you to
  iterate the HUD's layout with them — from the root, naturally.
- **`PROJECT_ROOT` was a captured default.** `project_root: Path =
  PROJECT_ROOT` binds at import, so monkeypatching the module global did
  nothing. The test caught it; the default is `None` now and the global is read
  at call time.

Verified by running both, not only by the suite: refused from the root,
allowed from `26augusztus/`, and `--hud-demo` still writes its PNG from the
root.
