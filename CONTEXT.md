# Puskás URH Kupa — the language

What each word in this project means, and nothing else. Component detail belongs
in ARCHITECTURE.md, research narrative in FINDINGS.md, the end-to-end story in
PIPELINE.md. If a term is defined here, the other documents *use* it — they do
not redefine it.

Only contested, overloaded or project-coined words are listed. Standard amateur
radio vocabulary (QSO, EDI, locator, band, mult) is assumed and deliberately
absent.

## Language

### Direction

Two concepts, one unit. Telling them apart is the difference between a number
you can compute days in advance and one that can be `null`.

**Bearing**:
The *initial* great-circle direction from one station to another, in degrees
true, derived from the two locators. A property of a **station pair** — fixed,
knowable before the round, never null. "Initial" is load-bearing: the bearing
changes along a great circle, and the value at your end is not the value at
theirs.
_Avoid_: azimuth (that is the rotator's), heading, direction

**Azimuth**:
Where the rotator is pointing **now**, in degrees true, as sampled from
rotctld. A property of **your own station over time** — live, and `null`
whenever the rotator is offline.
_Avoid_: bearing (that is the pair's), rotator angle, heading

### Occasion

**Contest**:
An amateur radio contest — the competition itself, spanning years. Never one
running of it.
_Avoid_: using this for a single running

**Event**:
One running of a contest. The general term, and the one the MRASZ API itself
uses.
_Avoid_: session, contest

**Round**:
One running of a *series* contest — Puskás runs monthly and MRASZ issues round
codes, so this is the natural word here. A series-flavoured synonym of Event;
for a one-off annual contest, say Event.
_Avoid_: session, contest

**Round start / round end**:
The time bounds of one round.
_Avoid_: session start, session end

### Connection

**Session** survives in this project as a word for *connections only*. It is not
the contest, not the terminal, not a recording.

**Session**:
A connection with state: the authenticated ON4KST session, an `IRCSession` (one
client connected to the bridge), the radio's own CI-V network session.
_Avoid_: using this for the round, the tmux session, or the cast

**tmux session**:
The tmux session that hosts the round's windows. Always spelled with `tmux`.
_Avoid_: bare session

**Cast**:
The asciinema recording of the logger pane, and the `.cast` file holding it.
_Avoid_: terminal session, screen recording

### Identity

**Callsign**:
The string identifying a station. **My callsign** when it is specifically our
own (`my_callsign`), plain Callsign for anyone else's.
_Avoid_: call (in code that can only mean invoking a function)

Three things keep the old spelling because they are not ours to rename: the
`"call"` key in `*-input.jsonl`, EDI's `PCall=`/`RCall=` fields, and the
`<MYCALL>`/`<HISCALL>` CW macro placeholders, which are contest-logger
convention everywhere.

**Station**:
The entity a callsign identifies. The harvested database holds stations you
*might* work; a QSO holds a callsign you *did*. Same string, different role.
_Avoid_: using this interchangeably with callsign

### Mode

Two vocabularies, and the step between them. `_mode_str` used to name both
ends of that step, in two files.

**Radio mode**:
What the radio reports it is doing — `USB`, `CW-R`, `FMN`, a dozen values,
spelled the way Icom spells them. `civ_mode_name` turns a CI-V code into one.
_Avoid_: mode (bare), when the radio's spelling is what is meant

**Logged mode**:
One of the three a contest log records: `SSB`, `CW`, `FM`. `mode_from_radio`
is the step from a Radio mode to this one; an unrecognised radio mode passes
through untranslated rather than being guessed at.
_Avoid_: mode family, normalized mode

### The scope stack

Four layers of one subject, deepest first. Each is a different kind of object —
a datum, a stream, a picture — and collapsing any two loses precision.

**Sweep**:
One spectrum sample from the radio: a single scan across the current span.
_Avoid_: frame, scan

**Scope**:
The capture of sweeps — the radio feature, the live stream, and the `.scope`
file. **Spectrum scope** is allowed when naming the radio's own feature, which
is Icom's term for it.
_Avoid_: spectrum (bare), waterfall

**Waterfall**:
Sweeps rendered as an image, time down one axis. The picture, not the data.
_Avoid_: spectrum, scope

**Spectrum**:
Not a domain term here. In ffmpeg's namespace it means an *audio* spectrum
(`showspectrum`), which is a different measurement entirely, so bare "spectrum"
in an RF context is always ambiguous. Say Sweep, Scope or Waterfall.
_Avoid_: all RF uses except the fixed phrase "spectrum scope"

### Side-channels

**Side-channels**:
Collectively, everything the logger writes during a round *besides the log*:
telemetry, the input log, the scope capture and the webcam capture. None of it
is recoverable afterwards, which is why they are named as a group.
_Avoid_: telemetry (as an umbrella)

**Telemetry**:
The rig and rotator stream only — `*-telemetry.jsonl`, one schema, one writer.
It does not cover the input log, the sweeps or the webcam.
_Avoid_: using this for any other side-channel

**Input log**:
Keystrokes and QSOs — `*-input.jsonl`.
_Avoid_: telemetry, keylog

### Coined here

**Segment**:
One WAV file from the radio's own Voice Recorder, split at every RX/TX
transition. The unit the audio timeline is assembled from.
_Avoid_: clip, chunk

**Mark**:
A timestamped value on the video timeline, e.g. the rotator azimuth at a given
moment. A mark may carry `None` — that is a real reading, not a gap.
_Avoid_: sample, point, keyframe

**Anchor**:
A time correspondence between the audio and the log that is confident enough to
fit a drift correction against.
_Avoid_: sync point, marker

**Recess**:
A cut-out in the HUD artwork that a live reading is drawn into. Sized by its
widest possible reading.
_Avoid_: slot, window, box

**PiP**:
A picture-in-picture inset in the rendered video — the cast and the webcam.
_Avoid_: overlay, inset

**Theme**:
The measured geometry of the HUD artwork — where each recess sits, in
`hud-theme/theme.json`.
_Avoid_: layout, skin

## Binding surface

Which terms are pinned to something outside this project, and to *what*. The
project is intended to serve contests beyond Puskás URH Kupa; this records how
narrow the actual coupling is, so the folklore does not outgrow it.

**Contest-bound** — changes with the contest, and this is the whole of it:
- the harvester's data source (`bb.mrasz.hu/nest`, MRASZ's event list)
- the sked message text in the bridge

**Radio-bound** — changes with the radio, not the contest:
- the band set 2M / 70CM / 23CM, and the EDI band map. These are the IC-9700's
  three bands.

**Band-bound** — changes with the bands worked:
- the ON4KST chat room (144/432 MHz)

Notably absent: scoring, multipliers and the round's start and end times are
nowhere hard-coded.

## Legacy names

Names on disk and in muscle memory that predate this glossary, kept
deliberately. They are *imprecise*, not *wrong* — do not "fix" them:

- `contest_video.py`, `run-recorded-contest-session.sh`, "contest directory" —
  all mean one **round**.
- `~/.puskas/`, `.puskas_cache/`, `puskas_logger.py`, `puskas_harvester.py` —
  the project's own prefix, not a statement that a tool is Puskás-only.
