# 15 — A thread map

Status: resolved

The logger is the only component here that is genuinely concurrent, and
nothing writes down what runs where. Threads today: the UI (main), the radio's
CI-V receive loop, the rig server's accept loop plus one thread per client, the
rotator poller, and the clock-sync one-shot. They meet on shared state guarded
by four separate locks (`_rig_lock`, `_rot_lock`, `_scope_rec_lock`,
`_telem_lock`), and the recorders are written from at least three of them.

Wanted: one map saying, per thread, what starts it, what it touches, which lock
guards that, and how it ends. The audience is someone about to move a global
between modules — which is exactly what issue 05 in `.scratch/structure/` is
doing to this file right now, and the reason the question came up.

Open questions before it can be written:

- **Where does it live?** ARCHITECTURE.md's `puskas_logger.py` section is the
  obvious home, but a table of who-touches-what goes stale silently — the same
  objection CLAUDE.md raises against justifying comments. Is there a form of
  this that a test can enforce?
- **Is a map the fix, or a symptom?** Five threads sharing seven module-level
  dicts through four locks may be the thing to change rather than to document.
  If each cluster ends up owning its state behind a module boundary (the
  direction the structure effort is already going), the map shrinks to a list
  of modules and the threads that call into each.

## Answer

Run as a one-time audit, not as a document to maintain. The map, the deadlock
analysis and the evidence are in FINDINGS.md under "Concurrency"; the rule they
support is in CLAUDE.md; the work that makes the code obey it is issue 16.

**How tangled: eight threads, seven locks, and one live deadlock.**

The structure turned out to be better than the count. An AST scan found no lock
nesting anywhere — nothing holds one lock while taking another — so a cycle
between two threads cannot form, and every `join()`/`wait()` is bounded by a
timeout. That leaves only self-reentrancy, and both instances were real: one
already known and fixed with an RLock, and one live on the round's *normal*
exit path.

The live one: a signal handler runs on the main thread wherever it happens to
be, the main thread is the UI, the UI is constantly inside `current_rig()`'s
critical section, and the handler's teardown took that same plain lock. SIGTERM
is how a round ends, so it ran every round. The failure is the exact one the
handler exists to prevent — the process hangs holding the radio session open,
and a second SIGTERM cannot kill it because that signal's handler is the thing
stuck. Fixed, with a red-before-green test and a 20,000-signal reproduction.

So the fear behind this ticket was well placed, but not where it looked. The
danger was never the *number* of threads; it was one thread being re-entered by
a signal.

**Can we live without threads: yes.** Everything concurrent here waits on I/O or
a timer and nothing waits on the CPU, prompt_toolkit is asyncio-native and the
logger already runs an event loop (`Application.run()` ends in `asyncio.run`),
and `on4kst_irc_bridge.py` already proves the pattern in this repo with 32
coroutines and no locks. Two of the eight get simpler rather than merely
different. It is a design principle now.

**Both open questions this ticket raised are answered by that.** Where does a
who-touches-what table live without going stale? Nowhere — it does not live at
all; it was evidence for a decision, so it is dated and filed in FINDINGS.md.
And "is a map the fix or a symptom?" was the right instinct: it was a symptom.
