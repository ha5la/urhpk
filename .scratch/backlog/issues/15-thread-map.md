# 15 — A thread map

Status: needs-triage

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
