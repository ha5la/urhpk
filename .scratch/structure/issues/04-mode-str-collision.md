# 04 — `_mode_str` means two different things

Status: ready-for-agent

Two functions, one name, unrelated concepts:

- `icom_net.py:133` — CI-V integer code to mode name (`0x03` → `CW`)
- `puskas_logger.py:215` — mode string to family (`USB` → `SSB`)

This is the inverse of duplication: a compressor would never merge these, it
would flag the symbol as overloaded. Same defect class as `azimuth`/`bearing`,
which also never met until one docstring used both words for one value.

Rename both to say what they are (`civ_mode_name`, `normalize_mode` or better),
and record both in `CONTEXT.md`. Both are `_`-prefixed and private, so the
blast radius is one file each plus tests.
