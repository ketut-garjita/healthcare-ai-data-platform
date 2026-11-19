"""Shared pytest policy for mandatory external-service test runs."""

import os

import pytest

_skipped_nodeids: list[str] = []


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail mandatory integration runs if any test was skipped."""
    if os.getenv("REQUIRE_INTEGRATION_SERVICES", "").lower() != "true":
        return
    if _skipped_nodeids:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        _skipped_nodeids.append(report.nodeid)
