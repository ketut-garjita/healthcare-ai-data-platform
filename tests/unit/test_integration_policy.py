from types import SimpleNamespace

import pytest

import conftest


def test_mandatory_integration_policy_fails_session_when_a_test_is_skipped(monkeypatch) -> None:
    conftest._skipped_nodeids.clear()
    conftest._skipped_nodeids.append("tests/integration/test_synthetic.py::test_service")
    monkeypatch.setenv("REQUIRE_INTEGRATION_SERVICES", "true")
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)

    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    conftest._skipped_nodeids.clear()
