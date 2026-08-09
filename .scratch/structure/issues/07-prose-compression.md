# 07 — Compress the prose

Status: resolved

Blocked by: 03

Long explanations shrink once the concept they describe has a name, so this
follows the library work rather than preceding it — extraction is what creates
most of the names.

Two passes:

- **Name the concept, then shorten every mention.** The pattern is
  "there's a problem when a lesson inside a section of a course is made real
  (i.e. given a spot in the file system)" becoming "there's a problem with the
  materialization cascade". Names go in `CONTEXT.md`; the mentions get short.
- **Delete comments that no longer say anything.** Especially comments
  justifying a duplication or a workaround that has since been removed — those
  argue against fixes that are already free. Issue 01 retires one of exactly
  this kind.

Line count is a proxy, not the goal: a comment explaining a hidden constraint
or a bug's root cause stays, however long. What goes is the essay where a
sentence works.

## Answer

Both passes ran, and each found something the other could not have.

**Delete comments that no longer say anything** — done by scan, not by eye.
Every identifier-looking word in every comment and docstring was checked
against where that name actually lives. Six were wrong:

- one named `_best_effort_logout`, a function renamed to `close()` long before
  this effort — the comment had been explaining a stash for a caller that does
  not exist under that name
- four attributed things to `contest_video.py` that issues 05 and 06 moved to
  `rig_state`, `webcam_sync` and `qso_windows`
- two attributed the webcam capture to `puskas_logger.py`, now `recorders.py`

No stale duplication-justifications survived; issue 01 retired the one this
ticket remembered. But the scan found the opposite — a comment *declaring* a
live duplication rather than justifying a dead one. `recorders`' and
`webcam_sync`'s capture-log parsers had byte-identical bodies and identical
regexes under different names, and one said it "mirrors" the other. The AST
scan behind issue 03 missed it because the names differ. It is `webcam_log.py`
now, verified on three real rounds against the microsecond stamp the logger
bakes into the mp4's own filename.

**Name the concept, then shorten every mention.** Four terms the code used
constantly and `CONTEXT.md` never defined: **over**, **burst**, **window**,
**trust gate**. Naming them paid twice.

It shortened prose directly — the trust gate's constants carried fourteen
lines explaining what a trusted decode is, and eight say it now.

And it exposed a synonym, which is the part worth remembering: `qso_windows`
called one concept both "burst" and "cluster", sometimes in the same sentence
("the real activity-burst ... the latest cluster start"). This is precisely
the azimuth/bearing defect, and it was invisible until the word had a
definition to be measured against. That is an argument for doing the naming
pass *before* looking for synonyms, not after — a glossary entry is what makes
a second word for the same thing visible as a defect rather than as style.

Line count was not the goal and did not move much. What moved is that a reader
following a cross-reference now arrives somewhere.
