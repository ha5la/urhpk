# 35 — A logger restart overwrites the round's LAN audio

Status: needs-triage

`261005-HA5LA-lan.wav` is 35 min of a 1 h 51 min round. Its sidecar starts at
`17:24:47` — two seconds after the restart (ticket 34). The first 75 minutes
were recorded and then destroyed.

Cause: the path is `{yymmdd}-{call}-lan.wav` (`puskas_logger.py`, at
`lan_audio_open`) and `LanAudioWriter` opens it with `"wb"`
(`urhpk/lan_audio.py`), sidecar with `"w"`. A second session on the same day
truncates both. The recording that exists as insurance for a crashed round is
exactly the one a restart deletes.

The webcam and the cast already survive a restart by carrying a timestamp in
the name (ticket 31 did this for casts).

## What to do

Name each session's file uniquely — a UTC stamp, like the webcam clips — and
never open an existing file for writing. Whether `contest_video.py` should
then read several LAN wavs is ticket 21's still-open question; it does not
read even one today.

Test: open the writer twice on the same day; the first file is intact.

## Comments
