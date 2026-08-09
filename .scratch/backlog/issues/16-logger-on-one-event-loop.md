# 16 — Put the logger on one event loop

Status: in-progress

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

## Progress

Done, each verified by the suite and by the pty smoke test:

1. **The UI runs on one event loop.** `run()` is a coroutine, `main()` drives it
   with `asyncio.run`, and the prompt is `prompt_async()`. The startup wizard's
   `input()` goes through `asyncio.to_thread`, because it polls `current_rig()`
   between prompts and blocking the loop would starve the radio it waits for.
2. **`_toolbar_watcher` is a task**, and the contextvars trap it carried is gone.
3. **`rig_server` is `asyncio.start_server`** — the accept loop and the
   thread-per-client are one handler coroutine. A test for two clients at once,
   awkward enough with threads that nobody had written it, now exists.
4. **`rotator` is two coroutines**, `_rot_lock` deleted. The rotctld wire format
   got its first tests, against a stand-in daemon.

The smoke test grew a probe that asks the running logger for the frequency on
4532, which is the only check that the server is served by the loop drawing the
UI.

## What is left: icom_net.py, and the radio thread that drives it

Everything still threaded is here — three threads (`_ctrl_loop`, `_civ_loop`,
`_meter_loop`), `_lock`, `_send_lock`, `_stop`, `_civ_ready` — plus the
logger's `_radio_thread`, `_rig_lock` and `_clock_sync_lock`, which cannot go
until the callbacks that write `_rig` and telemetry arrive on the loop.
`recorders`' two locks are in the same position.

Stopped here deliberately: this is the one part with hardware behaviour that
cannot be checked without the radio, and it is the logger's core function.

The design is worked out, and the shape is friendlier than it looks:

- **One `_UdpChannel(asyncio.DatagramProtocol)` per socket**, holding the
  transport and an `asyncio.Queue` of received datagrams, with
  `expect(match, timeout)` for the handshake. Open with
  `loop.create_datagram_endpoint(..., remote_addr=(host, port))` so sends need
  no address.
- **The handshake and the receive loop share that one queue and never
  overlap**, because the loop task only starts once the handshake returns —
  the same ordering the threaded version already has, minus the thread.
- **Every current `recvfrom`-with-timeout is `await chan.expect(...)`**, and
  both `_ctrl_loop` and `_civ_loop` have the identical shape:
  `await asyncio.wait_for(queue.get(), IDLE_PERIOD_S)`, handle, then the
  periodic idle/re-auth housekeeping — a direct translation.
- **Sends stay synchronous.** `transport.sendto()` never blocks, so `send_cw`,
  `stop_cw`, `set_clock` and `enable_scope` keep their current signatures and
  can still be called straight from a key binding. `_send_lock` goes away
  because the sequence-number increments stop being concurrent.
- `_stop` becomes task cancellation; `_civ_ready` becomes an `asyncio.Event`;
  `close()` becomes a coroutine that cancels the tasks and then says goodbye,
  preserving the ordering the comment on `enable_meters` insists on (no meter
  query may reach the wire after the goodbye).
- The CLI at the bottom gets an `asyncio.run`.

The 7 integration tests drive the real handshake against an in-process fake
radio, which is what makes this checkable without hardware — but they call
`connect()` synchronously and will convert with it. Expect them to be red
through the middle of the change; there is no useful intermediate green.

**Before a round**: this one needs a real session with the IC-9700, not just a
green suite. The abandoned-session behaviour (a half-registered session blocks
reconnecting for tens of seconds) is exactly the kind of thing the fake radio
does not model.
