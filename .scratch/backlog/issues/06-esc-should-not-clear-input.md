# 06 — Escape stops CW but must not clear the input box

Status: ready-for-agent

Pressing Escape to abort an in-progress CW message also clears whatever is
typed in the input box. Losing a half-typed callsign mid-QSO is exactly the
wrong moment for it.

Escape must call `_cw_stop()` and leave the buffer alone.
