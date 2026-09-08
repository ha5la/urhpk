# 25 — The CW macros died mid-round and nothing said so

Status: needs-triage

Seven minutes into the September round's second hour the F-key macros stopped
sending. The operator hand-keyed the rest of the QSO on the paddle, then
restarted the logger — which fixed it, and cost the round its single `.cast`
and single webcam clip (see the two-cast support that came out of this).

Nothing on screen said anything was wrong. The toolbar showed the band, the
frequency, the rotator, `● REC` and a ticking clock the whole time.

## What the round's own files say

Times are UTC, from `2026-sep/`.

**The last macro that worked was F7 at 17:06:14** — `?`, one 1.36 s
transmission on 144.080 CW. From `17:06:36` on, every transmission is
hand-sent: `CFM`, `73EE`, and at 17:11 an entire exchange sent in fragments —
`RR` / `5NN` / `TJ 0` / `TJ T` / `T1T` / `JN97TF`, twelve bursts over 80
seconds with two garbled attempts at the serial. F3 sends that in one burst.

**The radio stopped answering commands at 17:06:21.856**, the last meter
sample before a 412.6 s hole. Every other meter gap in the round is 5–10 s.
The hole ends at 17:13:14, four seconds after the restart.

The meter *loop* did not die: `on_radio_meters` writes one line per change,
so a frozen snapshot writes nothing at all. The snapshot it froze on
(`id 0, po 0, swr 0, vd 151`) is byte-identical to the first one recorded
after the restart — that row only exists because `forget_meters()` clears the
dedupe on reconnect. The radio simply stopped replying.

**It was not the network and not the session.** The scope stream ran the whole
time: 248,613 sweeps with exactly two gaps all round — 14.0 s at 15:58:27
(startup) and 13.8 s at 17:13:00 (the restart itself). Nothing at 17:06.
Frequency and mode transceive records keep arriving through the dead window
(173 of them — the operator turning the dial), as do the rotator's.

**A second command user broke the same way.** The clock monitor's `CLK` chip
goes to `—` at 17:11:03 and stays there, and its 17:11 `clock_offset_s` sample
is missing from a series that is otherwise every six minutes from 15:59 to
18:14. It is a different task on a different cadence, reading through
`_read_setting`. Both it and the meters wait on replies; both got none.

**The restart fixed it.** F3 at 17:17:51 sends `5NN T22 JN97TF` in one 9.65 s
burst. F1 at 17:42:51, 17:43:05, 17:43:19 and 17:43:33 is 10.26 s every time —
the same 10.26 s it took at 17:05:04 and 17:05:18, before the failure. That
identical duration is the radio's own message keyer; a paddle does not do that.

So: inbound push traffic kept flowing, outbound commands stopped being
answered, and `send_cw` (CI-V 0x17, fire-and-forget on that same path) went
into the void with them. That is the keyer "giving up".

## The part that is already a defect here, whatever wedged the radio

**The stall watchdog cannot see this failure, by construction.** `_civ_loop`
reopens the CI-V stream when nothing has arrived for `CIV_STALE_S` (2 s), but
`last_data` is set by *any* parsed CI-V data packet — and the scope sweeps are
CI-V data packets on that same socket, arriving continuously. They held
`last_data` fresh for all 412 seconds. `last_rx_age()`, documented as "the
liveness signal a reconnect supervisor needs", is inbound-only for the same
reason and would have reported a healthy session throughout.

Liveness is being measured on the half of the link that was never broken. A
session can answer nothing and still look perfectly alive, and did for seven
minutes.

## What is not the cause

`_civ_seq` and `_civ_inner` are both packed into 16-bit fields
(`_envelope`'s `<IHH`, `civ_data_packet`'s `>H`) and are never masked, so
`_send_civ_command` raises `struct.error` past 65535 — which would kill
`_meter_loop`'s unguarded `while True` outright and no-op every macro. It fits
the symptom exactly and **it is not what happened**: at the measured 5.04
sends/s the counter stood near 20,600 at 17:06:21. It is still a real bug
waiting at ~3.6 hours of unbroken session; this round's sessions were 1.1 h
each. Worth its own ticket, not worth chasing here.

## What to do

Ordered by what the next round gets out of it.

1. **Notice.** The meter loop already knows when a reply last landed. A
   command-path staleness signal — separate from `last_rx_age()`, which
   watches push traffic — and a toolbar chip when it goes stale. The operator
   should not find this out by hand-sending an exchange.
2. **Recover.** Reopening the CI-V stream is what the restart did the hard
   way. Once (1) exists the existing reopen path can be driven by it instead
   of by `last_data`.
3. **Find the trigger, if it can be found.** The meter poll is new this round
   and is the only steady outbound traffic — four commands back-to-back with
   no await between them, 5/s for an hour, alongside a ~30/s scope stream.
   Whether that is what wedges the radio's command handler is a guess; it is
   testable at the bench by running the poll with and without the scope
   enabled and waiting.

## Verification

(1) and (2) are testable against a fake transport: feed scope data
continuously, stop answering commands, assert the staleness signal fires and
the reopen follows — the case the current watchdog silently passes.

(3) is not testable that way and must not be faked. It is the real radio or
nothing, and a negative result over one bench session proves very little; this
happened once in 2 h 20 m.
