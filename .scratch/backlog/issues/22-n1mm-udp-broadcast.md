# 22 — Broadcast the round's QSOs as N1MM+ UDP

Status: needs-triage

Transmit side only: `puskas_logger.py` emits N1MM Logger+ style XML datagrams
so an off-the-shelf tool can watch the round live. Nothing is ever received —
the logbook stays the only writer of the log.

## Before anything is built: who consumes it

The consumer named at the outset was **GridTracker2, and the evidence says it
does not speak this protocol.** GridTracker2 listens on 2237 for the *WSJT-X*
UDP protocol (binary, `QSOLogged`/`Status`/`Decode`), forwards on 2238, and its
own docs mention N1MM nowhere; where the two appear together, GridTracker sits
in the middle of WSJT-X→N1MM traffic, with N1MM as the logger downstream of it,
not as its source. Its logging tab sends QSOs *out* to ACLog/Cloudlog-style
sites, and it calls itself a log viewer, not a logging program.

So there are two different tickets hiding here, and this is the first of them:

- **N1MM+ dialect** (this ticket) — consumers are PstRotator, Log4OM, DXLog,
  band-map and SDR displays. Cheap; see below.
- **WSJT-X dialect** — the one GridTracker2 would actually accept. Binary
  big-endian QDataStream framing, magic `0xadbccbda`, and a data model built for
  digital modes: it wants decodes and a `Status` with a DX call in it, and a
  hand-keyed SSB round has neither. Feeding it a `QSOLogged` per contact would
  populate GridTracker2's map and log view, and that is roughly the whole of
  what it would buy.

**Settle which map you want to watch before writing either.** If it is
GridTracker2's, this ticket is the wrong dialect and should be `wontfix`.

## Why, honestly

Weak on its face. The round already has its own rotator control, telemetry and
HUD, so nothing here is *needed* — the payoff is entirely "a second screen
running someone else's tool", and if no such tool is actually running during a
round, this is speculative code. It is on the list because it is unusually
cheap, not because it is unusually valuable.

## The protocol (n1mmwp.hamdocs.com, "External UDP Messages")

- Fire-and-forget XML datagrams. Unicast to `127.0.0.1:12060`, or broadcast to
  a `…255` subnet address on the same port. No handshake, no ack, no reply.
- `<contactinfo>` after a QSO is added; `<contactdelete>` (app, timestamp,
  mycall, band, call, contestnr, StationName, ID) for one removed;
  `<contactreplace>`, identical body to contactinfo, for one edited.
- `<RadioInfo>` for the rig's state: Freq, TXFreq, Mode, mycall, IsRunning,
  IsTransmitting, RadioNr, StationName.
- **Frequency is in units of 10 Hz** — 3525.19 kHz goes out as `352519`. The
  `<band>` field is a decimal string in MHz, and N1MM's own is localised
  (`3.5` or `3,5`); we emit the dot.

## What it maps onto here

| N1MM field | Here |
|---|---|
| `call`, `band`, `mode`, `timestamp` | `QSO` as it stands; `2M/70CM/23CM` → `144/432/1296` |
| `snt`/`sntnr`, `rcv`/`rcvnr` | `rst_s`/`nr_s`, `rst_r`/`nr_r` |
| `gridsquare` | `loc` |
| `points` | `QSO.points` |
| `mycall`, `stationprefix` | `LogBook.my_callsign` |
| `contestname` | a constant, `PUSKAS` |
| `ismultiplier1..3`, `zone`, `prec`, `ck`, `section`, `wpxprefix` | nothing — the exchange is RST + serial + locator, and these stay empty |
| `ID` | needed only if `contactdelete`/`contactreplace` are emitted |

## Where it hooks in

Three call sites, all of them already the place where the news arrives:

- `puskas_logger.py:1305`, `lb.add(qso)` → `contactinfo`, beside the existing
  `log_input_event`.
- `_on_radio_update` (`puskas_logger.py:121`) → `RadioInfo`, beside the existing
  `telemetry_write`.
- `LogBook.undo` → `contactdelete`, if IDs are carried.

Shape it like `recorders.py`: inert until opened, so a round that doesn't want
it never opens it. A `loop.create_datagram_endpoint(allow_broadcast=True)`
never blocks and needs no task, so the asyncio rule costs nothing here.

## Verification

The formatter is string-in/string-out and unit-testable; one socket round-trip
test with `wait_until` covers the transport. **Neither proves a consumer accepts
it** — same class of gap as ffmpeg branches and drawn frames. Point a real
consumer at it once and read what it shows, or the feature is untested.

## Constraints an implementation must not break

- Transmit only. Nothing on the wire may ever enter the logbook.
- A dead or unroutable socket is non-fatal and silent-ish: the round continues,
  exactly as with the audio capture in 21. This is the least important thing
  the logger does.
- No new dependency; `asyncio` and the stdlib are enough.
