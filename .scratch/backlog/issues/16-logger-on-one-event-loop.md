# 16 — Put the logger on one event loop

Status: resolved

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
costs a round. The pty harness sketched on issue 01 is the thing that
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

## Open questions — answered

- **Does `icom_net.py` keep a synchronous face?** No. It went async throughout
  and the CLI got an `asyncio.run`. Sends stayed synchronous, which is not a
  sync face over an async core but a property of UDP: `transport.sendto()`
  never blocks.
- **What blocks the loop that we have not noticed?** One thing, and it was not
  on the list: `webcam_toggle`'s stop branch waits up to 5 s for ffmpeg to
  finalize the mp4. It stays — see below.
- **Does anything still want `asyncio.to_thread`?** Only the startup wizard's
  `input()`, which was already the documented exception. Measured on a running
  logger: four OS threads, one event loop and three idle executor workers that
  wizard left behind.

## Progress

All five steps done, each verified by the suite and by the pty smoke test:

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
5. **`icom_net` is async throughout**, and with it the logger's radio task and
   the last four locks — see below.

The smoke test grew a probe that asks the running logger for the frequency on
4532, which is the only check that the server is served by the loop drawing the
UI.

## Step 5: icom_net.py, and the radio thread that drove it

Done as designed. `_UdpChannel(asyncio.DatagramProtocol)` per socket, holding
the transport and an `asyncio.Queue`, with `expect(match, timeout)` doing the
work every `recvfrom`-with-timeout used to. `_ctrl_loop` and `_civ_loop` are
tasks with the same shape they had; `_stop` is cancellation, `_civ_ready` is an
`asyncio.Event`, `connect()` and `close()` are coroutines, and the CLI got an
`asyncio.run`. Sends stayed synchronous, so the key bindings did not change.

The 7 integration tests converted with it — the fake radio moved onto the loop
too, since a threaded one waiting on `threading.Event` would have blocked the
loop the client under test needs.

Three things the design did not predict:

- **The throwaway-port trick went away.** `_local_udp_port()` existed only
  because conninfo names the CI-V port before that socket is opened. Opening
  the endpoint first and reading its port off the transport is the same
  sequence on the wire, one function shorter, and has no window for another
  process to take the port.
- **Cancelling the meter poller is not enough — awaiting it is.** Its four
  queries per cycle have no `await` between them, so the burst is atomic;
  `close()` awaits the cancelled tasks before the first goodbye packet.
- **The signal handler moved to `loop.add_signal_handler`**, which is what
  makes it safe for the teardown to touch the same state the UI touches. As a
  `signal.signal` handler it ran between two bytecodes of whatever the main
  thread was doing, which is half of the deadlock FINDINGS.md records.

## Also done: the logger side

`_radio_thread` is `_radio_task`; `_rig_lock`, `_clock_sync_lock` and
`recorders`' two locks are gone. Cancelling the radio task is now the whole
shutdown: its `finally` closes whatever session it reached, including one still
inside `connect()`, which is exactly the case `_radio_close_if_connected` needed
a thread join for.

**Not converted, deliberately**: the webcam `subprocess.Popen`. On Python 3.12
the default child watcher is `ThreadedChildWatcher`, which spawns a thread per
child — `asyncio.create_subprocess_exec` would have added the thread it was
meant to remove. Cost of leaving it: `proc.wait(timeout=5.0)` on the loop when
a recording is stopped, once per recording.

**Before a round**: this still needs a real session with the IC-9700, not just
a green suite. The abandoned-session behaviour (a half-registered session blocks
reconnecting for tens of seconds) is exactly the kind of thing the fake radio
does not model.

**Done**: `test2/` is that session — 2026-08-12, three days after the refactor
landed. Live CI-V telemetry (frequency, mode, rotator) and 2.7 MB of scope
stream, so the radio's session state machine was driven by the real thing.
