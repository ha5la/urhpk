# 04 — `_mode_str` means two different things

Status: resolved

Two functions, one name, unrelated concepts:

- `icom_net.py:133` — CI-V integer code to mode name (`0x03` → `CW`)
- `puskas_logger.py:215` — mode string to family (`USB` → `SSB`)

This is the inverse of duplication: a compressor would never merge these, it
would flag the symbol as overloaded. Same defect class as `azimuth`/`bearing`,
which also never met until one docstring used both words for one value.

Rename both to say what they are (`civ_mode_name`, `normalize_mode` or better),
and record both in `CONTEXT.md`. Both are `_`-prefixed and private, so the
blast radius is one file each plus tests.

## Answer

`icom_net._mode_str` → `civ_mode_name` (CI-V code to the radio's own spelling).
`puskas_logger._mode_str` → gone; the normalizer moved to `edi.mode_from_radio`,
because its three outputs *are* EDI's mode vocabulary. Both concepts are now in
CONTEXT.md as Radio mode and Logged mode.

The rename turned up a third copy the AST scan had missed: `contest_video`'s
`_SSB_ALIASES`, carrying a comment that it "matches puskas_logger.py's
_mode_str". It was a *subset* — it folded the SSB aliases only — so a `CW-R` or
`FMN` WAV title reached the video unfolded while the logger folded it. Folding
it in fixes that.

One behaviour change beyond the rename: an unrecognised mode now passes through
instead of being filed as SSB, so a firmware that invents a mode shows up in
the log rather than disappearing into it.
