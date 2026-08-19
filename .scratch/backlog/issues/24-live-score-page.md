# 24 — Publish the round's score live, on our own page

Status: needs-triage

A static page anyone can open during a round and watch the score move.
`puskas_logger.py` writes the score as JSON into a gist; the page reads it.

## Why it is ours and not a scoreboard's

The three live scoreboards — contestonlinescore.com, contest.run and the
scoredistributor.net relay that feeds both — key every post to a registered
Cabrillo contest ID, and Puskás URH Kupa has none. An unknown ID is refused
(`Error! Contest is closed or is not valid`, `Excluded contest`). COSB would
add one on request, but a scoreboard with one station on it is a table with one
row: no other HA entrant posts live scores today.

23 is the complement, not the alternative: Club Log shows the *QSOs* on a map,
this shows the *score*. Either can be built without the other.

## Shape

Two halves that meet at one URL, and only one of them moves during a round:

- **The page**: `index.html` on a `gh-pages` branch, deployed once and not
  touched again. Not in the gist — gists serve raw files as `text/plain`, so a
  browser will not render them. Not `master`, and not `docs/`, which holds this
  project's markdown and would be published wholesale.
- **The data**: one gist, reused every round, holding `score.json`. Updated by
  `PATCH https://api.github.com/gists/<id>` with
  `{"files": {"score.json": {"content": …}}}` — one `urllib` call, token in
  `~/.netrc` under `machine api.github.com`. `gh gist edit` in a subprocess is
  the other option and is rejected: it wants `gh` present and authenticated on
  the contest laptop at round time, for the same one request.

The page polls the raw gist URL with a `?t=<ms>` cache-buster. Two things it
must not do: poll `api.github.com` (60 requests/hour per viewer IP,
unauthenticated — a 30 s poll breaks for spectators), or trust the raw URL's
cache headers, which have been observed at 24 hours.

## What the JSON holds

Score, QSO count, per-band QSOs/points/mults, worked locators, the last QSO's
time, and `updated_at`. All of it exists already — `puskas_standings.py`
computes the same numbers for the HUD. The builder takes the logbook and
returns a dict; nothing new is calculated.

## Publishing rate

The concern is not the API's limits, it is not wanting a push every few
seconds. So: a publisher coroutine, woken by `lb.add`, that uploads **only when
the score changed** and **at most every 60 s**. A quiet hour is a handful of
requests, and a burst of QSOs is still one.

Latest-wins, not a queue: unlike 23, an older score is worthless the moment a
newer one exists. One slot, overwritten.

## Verification

The JSON builder is dict-in/dict-out and unit-testable against a logbook
fixture. The publisher gets a fake transport and a pinned clock: assert nothing
is sent when the score has not changed, assert the 60 s floor holds under a
burst, assert a failed upload is followed by a retry carrying the *newest*
score, not the one that failed.

**None of that proves a spectator sees anything.** Open the page from another
machine during a smoke round and watch it update — that is also the only way to
find out whether the cache-buster actually beats the CDN and whether the raw
URL's CORS headers allow the fetch at all. Both are assumptions until then.

## Constraints an implementation must not break

- The token never reaches the repo or the round's files — telemetry, input log
  and the video are published.
- Every failure is non-fatal and quiet. The round does not notice.
- Spectator-only. The EDI stays the truth, and nothing on this path feeds back
  into the logbook or the score.
- Inert until configured, like `recorders.py`: no gist id, no publisher, no
  credentials read.
