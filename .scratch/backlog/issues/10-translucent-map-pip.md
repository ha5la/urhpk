# 10 — Translucent map beside the cast PiP

Status: needs-triage

There is plenty of unused space next to the cast PiP. The vision: a
third-person aerial view of the QTH that **rotates with the antenna**, with a
pin dropped for each station worked — Google Earth-like.

Feasibility is the first question, and it splits into three: where the imagery
comes from and whether it can be embedded rather than fetched at render time;
whether rotating it per frame fits the render budget; and whether it reads as
anything at PiP size.

The data is already there — the rotator azimuth is a time series
(`hud_az_marks`) and every QSO carries a locator, hence a bearing and a
distance. Note the distinction: the map rotates with the **azimuth**, and the
pins sit at each station's **bearing**.
