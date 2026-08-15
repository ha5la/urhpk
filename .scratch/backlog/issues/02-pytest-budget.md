# 02 — Constrain pytest duration

Status: resolved

Set and enforce a limit on suite runtime.

Note the history: adopting `wait_until`/`recv_until` instead of guessed sleeps
already cut the suite from ~29 s to ~3.5 s and exposed a real race the old
slack had been hiding.

## Measured

On the operator's laptop (4 cores), 707 passed / 10 deselected:

| | |
|---|---|
| default run (coverage on) | 10.2 s |
| `--no-cov` | 7.6 s |
| `--collect-only` (import floor) | 1.4 s |
| slowest test | 1.12 s, `test_last_rx_age_is_fresh_while_connected_and_grows_when_radio_dies` |
| top 15 durations | ~3.5 s of the 7.6 s |

**1 s is not reachable** — imports alone cost 1.4 s. Coverage is ~25% of the
default run, and its share grows with the size of the source, not with test
slowness, so the budget is quoted against `--no-cov`.

## The shape

Two enforcement mechanisms, orthogonal: a per-test cap catches the failure mode
that actually happens (a guessed sleep, a wedged socket), a whole-suite ceiling
catches death by a thousand tests.

**Coverage moves out of `addopts`.** A `--no-cov` budget can never fire while
`pyproject.toml`'s `addopts` forces coverage on every run — pre-commit would
measure 10.2 s against a 7.6 s baseline. Move the `--cov=…` flags to an explicit
`uv run pytest --cov=…` step in `.github/workflows/test.yml`, which keeps the
Pages htmlcov artifact alive; plain `uv run pytest` and pre-commit become the
fast, budgeted run.

**Per-test cap**: `pytest-timeout` as a dev dependency, `timeout = 5` and
`timeout_method = "signal"` in `pyproject.toml`. 5 s is 4.5× the slowest honest
test, which measures an age growing over time and should not be shortened into
flakiness. The smoke tests cost ~20 s by design and take
`@pytest.mark.timeout(60)`.

**Suite ceiling**: a session-finish hook in `tests/conftest.py`, failing the run
above **12 s** and dumping the top 10 durations when it does — a ceiling that
only says "too slow" gets raised rather than fixed. It skips when `$CI` is set:
`ubuntu-latest` is 2–3× slower and far more variable than the machine the number
was calibrated on, and a flaky ceiling gets deleted within a month.

## Answer

Built as described. The default run is now **7.1 s for 712 tests** with no
coverage; `-m smoke` is 11 s for 10, not the ~20 s this ticket assumed, and its
slowest single test is 3.13 s — the `@pytest.mark.timeout(60)` override stands
anyway, since those tests start real processes and 5 s is only 1.6× today's
worst.

`budget_message` in `tests/conftest.py` holds every part of the decision and is
tested directly in `tests/test_suite_budget.py`; the hook around it only gathers
elapsed time, `$CI`, the mark expression and per-test durations summed across
setup/call/teardown. The ceiling also stands down for any selection other than
the default `-m 'not smoke'`, or a `-m smoke` run would trip it every time.

Verified against reality rather than only against the unit tests: with
`BUDGET_S` temporarily at 0, `uv run pytest tests/test_geo.py` printed the
message and exited **1** — `session.exitstatus` set from `pytest_sessionfinish`
does fail the run, and therefore pre-commit. A probe test sleeping 30 s died at
5.05 s with `Failed: Timeout (>5.0s) from pytest-timeout`.

## Not doing

`pytest-xdist` — complexity spent to beat a 7.6 s suite, and a parallel run hides
the ordering-dependent races this project has already been bitten by. Nor
surgery on the icom_net integration tail: getting to 4 s means making honest
timing tests less honest.
