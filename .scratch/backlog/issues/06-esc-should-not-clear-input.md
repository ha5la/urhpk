# 06 — Escape stops CW but must not clear the input box

Status: resolved

Pressing Escape to abort an in-progress CW message also clears whatever is
typed in the input box. Losing a half-typed callsign mid-QSO is exactly the
wrong moment for it.

Escape must call `_cw_stop()` and leave the buffer alone.

## Answer

One line deleted: `_on_escape`'s final `else` branch cleared the buffer.
`_cw_stop()` and the completion-cancel branch stay; so does the edit-mode
branch, where clearing *is* the cancel.

Covered by `test_escape_keeps_a_half_typed_callsign` in the smoke suite — the
keybindings live inside `run()`'s closure, so the pty is the only seam that
reaches them. Type a callsign, press Escape, finish the QSO: red before the
fix with `Invalid callsign: '599'`, green after.

Note for anyone writing another key test there: `ttimeoutlen` is 50 ms, so a
lone `\x1b` needs a gap after it or the next character makes it an Alt chord.
