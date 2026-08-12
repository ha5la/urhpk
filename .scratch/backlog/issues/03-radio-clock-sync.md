# 03 — Zero clock difference between radio and laptop

Status: resolved

Goal: no drift between the radio's clock and the laptop's for the whole round.
Over rigctld only minute precision was reachable, and even that was slow and
buggy. Alt+T is unreliable when the clock is only 2-3 s off.

Investigate: does the radio do NTP? Can `icom_net.py` set the clock directly
(it already has `civ_clock_payloads`, CI-V command 0x1A, and the IC-9700
ignores seconds)? Verify against the radio's own menu, not against the command
appearing to succeed.

This matters more than it looks: every timestamp in the pipeline is joined on
the assumption that the two clocks agree.

## Answer

**Yes, the radio does NTP** — and it had been failing silently since the
beginning, which was the whole cause of the drift. Settings found by scanning
the numbered parameters next to the clock ones: `1A 05 0181` NTP Function (ON),
`1A 05 0182` NTP server address, factory value `time.nist.gov`. The radio's
only link is the cable to the laptop, so that server was never reachable.

**Fixed by making the laptop the radio's time source.** `chronyd` now serves
the radio's subnet (`/etc/chrony/conf.d/radio-ntp.conf`: `allow
192.168.125.0/24` + `local stratum 10`), and the radio's `0182` points at
`192.168.125.1`. A clock left deliberately 30 s wrong corrected itself to
**+0.014 s** with nothing pressed. Drift with it working: four samples over
18 minutes spanned 19–49 ms, no trend.

**Verification did not use the radio's menu** — something better turned up.
The radio reports only HH:MM, so its seconds are readable exactly once a
minute: poll `1A 05 0180` fast and record when the reported minute increments.
That instant is the radio's `:00`, to a few tens of ms. Every number above was
measured that way, against the real radio.

**The old folklore was wrong.** A CI-V time-set is never ignored: it zeroes the
radio's seconds counter when the frame lands (sending the minute the radio
already displayed, at laptop `:30`, moved its rollover to `:30.016`). The "set
silently doesn't take when only 2-3 s off" belief comes from verifying with
`get_clock`, whose seconds field reads zero regardless of the truth.

## What shipped

- **Alt+T is gone**, along with `set_clock`/`civ_clock_payloads` and their
  tests — NTP replaces it, and dead code is debt.
- **`_clock_monitor_run` in `puskas_logger.py`**: hunts the first rollover at
  1 Hz, then predicts each next one and asks in a 20 Hz burst around it — 53
  queries per 200 s against ~200 for a continuous poll, and ±25 ms instead of
  ±0.5 s. A burst that misses means the clock stepped, so it re-acquires.
- **Toolbar `CLK` chip**: `CLK -0.2s`, `CLK —` before the first rollover,
  yellow past `CLOCK_WARN_S` (2 s).
- **`clock_offset_s` in `*-telemetry.jsonl`**, every few minutes, so a round's
  clock agreement can be checked afterwards instead of remembered.
- `icom_net.read_clock()` + `parse_clock_reply` + `clock_offset_s`, with
  `read_param` refactored onto the shared `_read_setting`.
- Docs: FINDINGS.md gains "The radio's clock"; RECORDING.md's clock section
  rewritten; PIPELINE.md and ARCHITECTURE.md updated.

Verified end to end against the real radio: the shipped monitor produced
`CLK -0.2s` and `{"clock_offset_s": -0.19}`, sign and magnitude consistent
with the 20 Hz probe.

## Comments

**Left alone deliberately**: FINDINGS.md's August 2026 thread audit still lists
`_clock_sync_lock` and "one [thread] for the startup clock sync". That section
is a dated snapshot of the pre-asyncio design, not a description of current
code; editing it would falsify the record it exists to keep.

**Still unknown**: how often the radio re-polls NTP on its own. An off→on
toggle of `0181` forces an immediate poll, and it did not visibly re-poll
during the ~25 minutes before that toggle. It does not matter at the measured
drift rate (<50 ms over 18 min), and the `CLK` chip would show it if it ever
did — but a round that starts with the chip near zero is not, strictly, proof
that the radio will re-sync during it.
