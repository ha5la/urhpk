# 01 — Drop scope_preview.py

Status: resolved

`scope_preview.py` (188 lines) renders a `.scope` recording into a standalone
waterfall video. It was a one-time tool for looking at scope captures before
`contest_video.py --scope` existed; that path now covers the same ground inside
the real pipeline.

Removing it is not only a deletion — it is the cheapest dedup in this effort:

- It is the **only other holder** of `SCOPE_AMP_MAX` and
  `SCOPE_WATERFALL_SPAN_S`, two of the four constants duplicated across files.
- It is the **only other holder** of the waterfall colormap gradient.
- `contest_video.py` carries a comment *justifying* that gradient copy — "kept
  as a separate copy rather than a shared import, since scope_preview.py is a
  standalone preview tool" — which becomes false the moment the tool is gone.
  A stale justification is worse than no comment: it argues against a fix that
  is now free.

## Scope

- Delete `scope_preview.py`
- `README.md` — remove its row from the components table
- `ARCHITECTURE.md` — the `.scope` reader is no longer read by two consumers
- `icom_net.py` — same, in the `.scope` format comment
- `contest_video.py` — retire the comment defending the duplicated gradient

Untracked scratch outputs in the working tree (`scope_preview.mp4`,
`preview_source.scope`) are not the repo's business and are left alone.

## Done when

Nothing references `scope_preview`, the suite passes, and no comment claims a
copy exists for a reason that no longer holds.
