# 23 — Push each QSO to Club Log in real time

Status: needs-triage

`puskas_logger.py` POSTs one ADIF record per QSO to Club Log's real-time API,
so `clublog.org/livestream/HA5LA` shows the round as it happens: a map of the
worked grids, the last 30 contacts, a rate figure.

## What this is not

**Not a live score.** Live Stream is a QSO feed — no points, no multipliers, no
ranking against other entrants. The scoreboards that do show a score
(contestonlinescore.com, contest.run, both fed via scoredistributor.net) key
every post to a registered Cabrillo contest ID, and Puskás URH Kupa has none;
an unknown ID is rejected outright. Publishing the round's *score* is a
separate problem with a separate answer (a static page we host ourselves), and
it is not this ticket.

So the payoff here is spectators watching the round unfold, and nothing else.

## The API

`POST https://clublog.org/realtime.php`, form-encoded:

| Field | Value |
|---|---|
| `email` | the Club Log account |
| `password` | an **Application Password**, not the account one |
| `callsign` | `LogBook.my_callsign` |
| `api` | API key, from account settings |
| `adif` | exactly one record, ending in `<EOR>` |

Credentials belong in `~/.netrc` under `machine clublog.org`, like the chat and
the radio; the API key is not a `.netrc` field, so it needs a home — simplest is
the `account` field of the same entry.

Live Stream is off by default: Settings → Live Stream → enable, once, by hand.

The record is built from fields the QSO already has: `CALL`, `QSO_DATE`,
`TIME_ON`, `BAND`, `MODE`, `RST_SENT`, `RST_RCVD`, `STX`/`SRX`, `GRIDSQUARE`
from `loc`, `MY_GRIDSQUARE` from the locator fetched at startup.

## Hazards, in the order they will bite

- **Never batch.** The endpoint is per-QSO at operating pace; a loop that
  uploads a backlog of records back-to-back trips the throttle.
- **Repeated auth failures get the IP firewalled** by Club Log — their words. A
  403 must stop the uploader for the rest of the round, not retry.
- **Edits do not propagate.** There is no update or delete in this API. A call
  corrected after upload stays wrong in the stream, and `undo` leaves a QSO
  that no longer exists in the log. The stream is best-effort; the EDI stays
  the truth, and a full log upload afterwards is what fixes the record.
- **The round's internet is a hilltop's internet.** Uploads queue and drain,
  and the queue is bounded and dropped-oldest — a round is short and a stale
  QSO is worth less than the memory.

## Where it hooks in

One call site: `lb.add(qso)` in `puskas_logger.py`, beside the existing
`log_input_event`. Shape it like `recorders.py` — inert until opened, so a
round that does not want it never opens it, and no credentials are read.

A queue plus one drain coroutine, per the asyncio rule. No new dependency:
`urllib.request` as everywhere else here, and the one blocking call goes in
`asyncio.to_thread` at the boundary — the escape hatch, holding no lock.

## Verification

The ADIF formatter is string-in/string-out and unit-testable. The uploader gets
a fake transport: assert one POST per QSO, assert the 403 latch stops it, assert
the queue survives a failed send and drains on the next success — polled with
`wait_until`, not slept on.

**None of that proves Club Log accepts the record.** Upload one real QSO to the
live account once and read the stream back, or the feature is untested.

## Constraints an implementation must not break

- Transmit only. Nothing Club Log returns may enter the logbook.
- Every failure is non-fatal and quiet: no network, bad credentials, or a 500
  changes nothing about the round. This is the least important thing the logger
  does, on a par with 21 and 22.
- No credentials in the repo, and none in the round's files either — the
  telemetry and input logs are published with the video.
