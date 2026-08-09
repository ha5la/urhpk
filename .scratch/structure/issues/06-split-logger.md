# 06 — Split puskas_logger.py (2,130 lines)

Status: claimed

Blocked by: 03

Candidate seams: the EDI logbook, the locator cache, the rig server, the
telemetry/input/scope recorders, CW macro expansion, and the prompt_toolkit UI.

The UI resists extraction and should probably stay whole: it is verified by
running rounds rather than by tests, and "no visual glitches" is a real
requirement that no unit test asserts.
