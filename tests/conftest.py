"""The suite's own runtime ceiling.

A budget is only useful if something enforces it. The per-test cap is
`pytest-timeout`'s, configured in pyproject.toml; this is the other half, the
whole-suite ceiling that catches death by a thousand tests.

The ceiling stays quiet in CI: `ubuntu-latest` is a shared runner, several times
slower and far more variable than the machine the number was measured on, and a
ceiling that trips at random is a ceiling somebody deletes.
"""

import os
import time

import pytest

BUDGET_S = 12.0
DEFAULT_MARKEXPR = "not smoke"


def budget_message(
    elapsed: float,
    durations: list[tuple[str, float]],
    *,
    ci: bool,
    markexpr: str,
    budget: float = BUDGET_S,
) -> str | None:
    """Why this run blew the budget, or None if it did not."""
    if ci or markexpr != DEFAULT_MARKEXPR or elapsed <= budget:
        return None
    slowest = sorted(durations, key=lambda d: d[1], reverse=True)[:10]
    lines = [f"suite took {elapsed:.1f}s, budget {budget:.0f}s"]
    lines += [f"  {seconds:6.2f}s {name}" for name, seconds in slowest]
    return "\n".join(lines)


_started = 0.0


def pytest_sessionstart(session: pytest.Session) -> None:
    global _started
    _started = time.monotonic()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != 0:  # a failing run already fails; don't bury it
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    per_test: dict[str, float] = {}
    for reports in reporter.stats.values():
        for report in reports:
            if hasattr(report, "duration"):
                per_test[report.nodeid] = (
                    per_test.get(report.nodeid, 0.0) + report.duration
                )
    message = budget_message(
        time.monotonic() - _started,
        list(per_test.items()),
        ci=bool(os.environ.get("CI")),
        markexpr=session.config.option.markexpr,
    )
    if message:
        reporter.write_line(message)
        session.exitstatus = 1
