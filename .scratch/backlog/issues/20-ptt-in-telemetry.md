# 20 — PTT in telemetry, to label the meter readings

Status: needs-triage

The S-meter slot switches to Po on transmit — in the protocol, not just on the
display: `15 02` stops being queried during TX and `15 11` starts (FINDINGS.md).
So anything consuming the recorded meters needs to know PTT to label a reading,
and telemetry currently has no ptt field.

Scope is exactly that. Two things this is **not**:

- **Not a source of truth for ptt.** That stays the WAV metadata, permanently: a
  PTT transition is what cuts the file, so the fact and its timestamp are the
  same event. See ARCHITECTURE.md's provenance table.
- **Not a clock-sync instrument.** Correlating PTT against WAV boundaries to
  measure the radio↔laptop offset was considered and rejected — PTT cannot be
  had unsolicited (poll-only at 1–3 Hz), and the radio-side stamp is quantised
  to a whole second, so it cannot beat the rollover burst already recorded as
  `clock_offset_s`. FINDINGS.md has the reasoning.

A new name is wanted rather than reusing `ptt`, so that a reader cannot mistake
it for the authoritative one. `tx` is the obvious candidate.

Blocked by nothing, but worth little until something actually consumes the
meters — the HUD's Vd/Id readouts do, but they are not the pair that switches.
