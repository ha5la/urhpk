# 34 — Reopening the CI-V stream does not revive a dead command path

Status: needs-triage

Ticket 25's (1) and (2) shipped before the October round. (1) worked, (2) did
not: the macros died again and only restarting the logger brought them back.

## What the round's own files say

Times are UTC, from `2026-oct/`.

- **17:21:59–17:22:01** — F3 for HG5O on 432.080 CW. The meters show full
  power for ~1 s (`po 212`, then `id 26` falling at 17:22:01.184); F3 normally
  runs ~9.6 s, so this macro was **aborted**. The cast has two off-tick redraws
  144 ms apart at 17:22:00.445/.589, and another pair at 17:22:01.457/.603.
  Neither changes anything visible, which is what an Esc with no completion
  and no edit in progress looks like — it fits the operator's memory of a
  double Esc. Esc presses are not in the input log, so this is inference.
- **17:22:12.233** — the last meter reply. Replies kept arriving for **~11 s
  after** the abort, so the Esc was not instantly fatal.
- **17:22:17.031** — `CIV ✗` appears in the cast, exactly `CMD_STALE_S` later.
  The notice works.
- **17:22:17 → 17:24:34** — `CIV ✗` stays red for 2 min 17 s. `_civ_loop`
  reopens every `CIV_STALE_S` while stalled; none of those reopens brought a
  single reply back. The scope stream had no gap anywhere near (its only gaps
  in the round are 2.4 s at 16:22:13 and the 12.2 s restart at 17:24:35).
- **17:24:36** — the restart. Meters come back immediately.

September looks the same from the outside: a ~1 s CW transmission (F7, `?`),
then the last reply 7 s later. Its cast shows no clear Esc pair, so Esc is not
established as the common factor. What both have is **a CW message ending
seconds before the radio stops answering** — against dozens of macros in each
round that did not do it.

## So: the double Esc?

Possible, not shown. Each Esc calls `_cw_stop()` → `0x17 0xFF` on the same
fire-and-forget path as `send_cw`, so a double Esc is two stop commands, one
of them while the keyer is already idle. The 11 s delay and September's case
argue against it being the whole story. Only the bench can settle it.

## What to do

1. **Recover the way the restart does.** A CI-V stream reopen is not enough;
   a full session reconnect (the path the logger's reconnect supervisor uses
   after `RADIO_RECONNECT_S`) is what fixed it twice. Escalate to it when the
   command path stays stale past a few reopens. Testable on the fake
   transport: a radio that ignores CI-V reopens and only answers after a new
   login.
2. **Bench**: with meter poll and scope on, F-key + Esc-Esc in a loop, and
   separately F-key + single Esc, and short complete macros (F7). Whichever
   wedges it is the trigger. A negative run proves little; record the count.
3. **Log Esc / F-keys to the input log** (`cw_send`, `cw_stop` events), so the
   next occurrence answers "what was pressed" from the file instead of from
   redraw timing.

## Comments
