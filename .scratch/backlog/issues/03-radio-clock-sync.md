# 03 — Zero clock difference between radio and laptop

Status: needs-triage

Goal: no drift between the radio's clock and the laptop's for the whole round.
Over rigctld only minute precision was reachable, and even that was slow and
buggy. Alt+T is unreliable when the clock is only 2-3 s off.

Investigate: does the radio do NTP? Can `icom_net.py` set the clock directly
(it already has `civ_clock_payloads`, CI-V command 0x1A, and the IC-9700
ignores seconds)? Verify against the radio's own menu, not against the command
appearing to succeed.

This matters more than it looks: every timestamp in the pipeline is joined on
the assumption that the two clocks agree.
