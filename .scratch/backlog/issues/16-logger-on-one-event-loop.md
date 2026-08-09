# 16 — Put the logger on one event loop

Status: ready-for-human

Blocked by: 01 (end-to-end test)

CLAUDE.md now says concurrency here is asyncio, not threads. `puskas_logger.py`
and `icom_net.py` do not follow it yet — this is the ticket that makes the rule
true of the code. The audit behind the rule, including the deadlock it found, is
in FINDINGS.md under "Concurrency".

The target is one `asyncio.run()`, `await session.prompt_async()` inside it, and
every current thread as a task. Eight threads and seven locks go to zero locks.

| Today | Becomes |
|---|---|
| main / prompt_toolkit | `await session.prompt_async()` |
| `_radio_thread` | task, `await asyncio.sleep` for the reconnect cooldown |
| `icom_net._ctrl_loop`, `_civ_loop` | `loop.create_datagram_endpoint` |
| `icom_net._meter_loop` | task, `await asyncio.sleep(METER_POLL_S)` |
| `rotator.poll_thread` | `asyncio.open_connection` + `asyncio.wait_for` |
| `rig_server.serve` + thread-per-client | one `asyncio.start_server` |
| `_toolbar_watcher` | task; `invalidate()` needs no contextvars workaround |
| `_clock_sync`, `rotator.point_at` one-shots | `asyncio.create_task` |
| webcam `subprocess.Popen` | `asyncio.create_subprocess_exec` |

## Why it is blocked

Not on anything technical. Every step above has a direct API, and the bridge
already proves the pattern in this repo. It is blocked because the logger's UI
is verified by running rounds, this touches every thread it has, and a mistake
costs a contest round. The pty harness sketched on issue 01 is the thing that
makes it checkable; do that first.

## Order, if it runs

`icom_net.py` is the bulk of the work and the part with real hardware
behaviour, so it goes last, behind the parts that can be verified offline:

1. `rig_server` — smallest, fully covered by existing tests, and `start_server`
   deletes the thread-per-client outright
2. `rotator` — one poll loop, one one-shot command
3. the recorders' file writes — plain awaited appends, locks gone
4. the UI: `main` becomes `async def`, `prompt()` becomes `prompt_async()`,
   `_toolbar_watcher` becomes a task
5. `icom_net` — the two UDP loops and the meter poller, against the real radio

## Open questions

- **Does `icom_net.py` keep a synchronous face?** It has a standalone CLI and
  its own integration tests with a mock radio. Either it goes async throughout
  and the CLI gets an `asyncio.run`, or it keeps a thin sync wrapper. Prefer the
  former; a sync wrapper over an async core is how the two-worlds problem
  starts.
- **What blocks the loop that we have not noticed?** The recorders' `write` +
  `flush` per event, and `open()` on first use. These are small local appends
  and are probably fine, but "probably fine" on the loop that draws the UI is
  worth measuring once rather than assuming.
- **Does anything still want `asyncio.to_thread`?** Nothing found in the audit,
  which is worth re-checking rather than trusting: if the answer really is
  nothing, the logger ends with no threads at all.
