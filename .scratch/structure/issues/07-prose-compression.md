# 07 — Compress the prose

Status: needs-triage

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
