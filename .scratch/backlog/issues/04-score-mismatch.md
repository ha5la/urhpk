# 04 — My computed score disagrees with the server's

Status: needs-triage

The server is the authority and reports 34 × 2m QSOs = 2352 points, 22 × 70cm
= 937 points. Our own figure differs — probably rounding, but that is a guess.

Approach: datamine previous scores from the server the way `puskas_harvester.py`
already mines claimed rounds, and find where the difference enters. Done when
our number equals the server's for every past round we can fetch, not just for
this one.
