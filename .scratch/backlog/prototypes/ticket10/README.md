# Ticket 10 prototypes

The throwaways behind ticket 10 and ticket 33, kept because both tickets quote
numbers that only these produced. They are archived **exactly as they ran** —
not tidied, not linted (`pyproject.toml` excludes this directory), because a
record that has been edited is no longer the thing that produced the numbers.

They were written against `/tmp/ticket10/` and still read and write there:

    mkdir -p /tmp/ticket10
    cd /home/kod/code/urhpk
    uv run python .scratch/backlog/prototypes/ticket10/curve_px.py

Each script puts its own `sys.path` entries in, so running one from anywhere
in the project works. The imagery and DEM tile caches (`tiles-s2/`,
`tiles-dem/`, several hundred MB) are **not** archived; anything that needs
them refetches on first run.

## What produced what

| Script | What it settled |
|---|---|
| `drone.py`, `path_view.py` | the original camera geometry, on synthetic then real imagery |
| `twolevel.py`, `aniso.py` | two texture levels; the anisotropic filter and the shimmer it fixed |
| `swing.py`, `swing2.py`, `swing3.py` | the move between stations |
| `basemap.py`, `mock.py`, `crop.py` | the rejected top-down outline map |
| `terrain.py` | the DEM raymarch — the rejected terrain |
| `terrain_sheets.py`, `terrain_close.py`, `terrain_skyline.py` | terrain at the settled camera, close up, and against the sky |
| `curve_bench.py` | curvature's cost: +83 ms, +15% |
| `curve_px.py` | curvature's displacement in pixels, against terrain's |
| `curve_look.py`, `curve_look2.py` | curvature rendered through the production renderer |
| `reframe.py`, `reframe_look.py` | the camera solved from QTH-at-90% and horizon-at-10% |
| `horizon_gain.py`, `horizon_gain2.py` | whether terrain amplifies near the horizon (it does not) |
| `skyline.py`, `skyline_sheet.py` | supersampling, borders and terrain together |
| `haze_test.py` | that haze, not geometry, is what hides terrain |
| `shadow_test.py` | that hillshade cannot rescue it |
| `preview_pairs.py`, `preview_horizon.py` | the before/after previews, at both cameras |
