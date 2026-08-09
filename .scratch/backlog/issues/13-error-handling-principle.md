# 13 — An error-handling principle

Status: needs-info

Hintjens' argument in the ZeroMQ Guide, in shape: don't try to handle every
error where it happens. Assert what must be true, let a process die when its
assumptions break, and put recovery at a level that can actually recover — a
supervisor, a reconnect loop. Living systems tolerate component death; they do
not ask each cell to survive everything.

## Why it fits here

This repo has **40 `except Exception` blocks, 32 of which swallow the error
entirely** (`pass`, `continue`, bare `return`). That is a lot of silent
failure in a stack whose whole purpose is that a round is captured well enough
to reconstruct afterwards. An exception swallowed during a round is data that
does not exist later, and nothing says so at the time.

The geo work in `.scratch/structure/issues/03` deleted six of them and lost
nothing: every one was standing in for "this locator might not parse", which
the type now says out loud.

## Why it needs a boundary before it becomes a principle

Blanket fail-fast is wrong for the logger. Dying at 18:47 costs the round, and
there is no supervisor for it — `hamlib_supervisor.py` restarts rotctld, not
the logger, and ARCHITECTURE.md states outright that the radio thread must
never die.

So the useful form is not "crash on everything" but two rules:

- **Recovery belongs where recovery is possible** — a supervisor or a
  reconnect loop, not the call site.
- **A swallowed error is a decision, not a reflex.** If it is swallowed, the
  reason is written down, and something is louder than nothing: a log line, a
  toolbar indicator, a counter.

## Decide

Whether to add this to CLAUDE.md's principles, and in what words. Then a
sweep of the 32 — most likely outcome is that a good number narrow to a
specific exception type and the rest gain a reason.
