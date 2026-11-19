from unittest.mock import MagicMock

import pytest

from healthcare_platform.ingestion.fhir import (
    FhirIngestionService,
    content_hash,
    parse_last_updated,
    validate_resource,
)
from healthcare_platform.integrations.fhir.client import FhirError

PATIENT = {"resourceType": "Patient", "id": "synthetic-patient-001", "active": True}


def test_hash_is_stable_for_object_key_order() -> None:
    assert content_hash({"resourceType": "Patient", "id": "x"}) == content_hash(
        {"id": "x", "resourceType": "Patient"}
    )


@pytest.mark.parametrize(
    "resource",
    [
        {"resourceType": "Patient"},
        {"resourceType": "NotSupported", "id": "x"},
        {"resourceType": "Patient", "id": "../unsafe"},
        {},
    ],
)
def test_validate_rejects_invalid_resource(resource: dict) -> None:
    with pytest.raises(FhirError):
        validate_resource(resource)


def test_validate_supported_resource() -> None:
    assert validate_resource(PATIENT) == ("Patient", "synthetic-patient-001")


def test_identical_hash_results_are_deterministic() -> None:
    assert content_hash(PATIENT) == content_hash(PATIENT.copy())


def test_source_provenance_removes_url_credentials_and_query_secrets() -> None:
    service = FhirIngestionService(
        "unused", MagicMock(), "https://user:secret@example.test/fhir?token=hidden"
    )
    assert service.source == "https://example.test/fhir"
    assert "secret" not in service.source and "hidden" not in service.source


def test_last_updated_requires_timezone_and_normalizes_to_utc() -> None:
    parsed = parse_last_updated({"meta": {"lastUpdated": "2025-01-01T03:00:00+03:00"}})
    assert parsed is not None and parsed.isoformat() == "2025-01-01T00:00:00+00:00"
    assert parse_last_updated({"meta": {}}) is None
    with pytest.raises(FhirError, match="include a timezone"):
        parse_last_updated({"meta": {"lastUpdated": "2025-01-01T00:00:00"}})
    with pytest.raises(FhirError, match="timezone-aware"):
        parse_last_updated({"meta": {"lastUpdated": "not-a-time"}})


def test_ingestion_records_failure_without_payload_in_response(monkeypatch) -> None:
    store = MagicMock()
    service = FhirIngestionService("postgresql://invalid", store, "test-source")
    # The failure must be explicit if the audit database is unavailable.
    result = service.ingest({"resourceType": "Patient", "id": ""})
    assert result["status"] == "audit_failed"
    assert "Patient" not in result["error"]
    store.put_raw.assert_not_called()


class FakeConnection:
    def __init__(self, events: list[str], fail_sql: str | None = None) -> None:
        self.events = events
        self.fail_sql = fail_sql

    def __enter__(self):
        return self

    def __exit__(self, exception_type, *_):
        self.events.append("rollback" if exception_type else "commit")

    def execute(self, query: str, _params=()):
        if "ON CONFLICT (ingestion_id) DO UPDATE SET status='failed'" in query:
            self.events.append("audit_failure_update")
        if self.fail_sql and self.fail_sql in query:
            raise RuntimeError("simulated database failure")
        if "SELECT status FROM fhir_ingestion_attempts" in query:
            return FakeCursor(("processing",))
        if "SELECT content_hash FROM fhir_resources" in query:
            return FakeCursor(None)
        return FakeCursor(None)


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.rowcount = 1

    def fetchone(self):
        return self.row


def test_attempt_is_committed_before_silo_write(monkeypatch) -> None:
    import healthcare_platform.ingestion.fhir as fhir_module

    events: list[str] = []
    monkeypatch.setattr(
        fhir_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(events),
    )
    store = MagicMock()
    store.ensure_bucket.side_effect = lambda: events.append("ensure_bucket")
    store.put_raw.side_effect = lambda *_args: events.append("put_raw")

    result = FhirIngestionService("unused", store, "test-source").ingest(PATIENT)

    assert result["status"] == "ingested"
    assert result["outcome"] == "created"
    assert events.index("commit") < events.index("put_raw")


def test_failed_attempt_creation_stops_before_silo_write(monkeypatch) -> None:
    import healthcare_platform.ingestion.fhir as fhir_module

    events: list[str] = []

    def connect(*_args, **_kwargs):
        return FakeConnection(events, fail_sql="INSERT INTO fhir_ingestion_attempts")

    monkeypatch.setattr(fhir_module.psycopg, "connect", connect)
    store = MagicMock()
    result = FhirIngestionService("unused", store, "test-source").ingest(PATIENT)

    assert result["status"] == "audit_failed"
    store.put_raw.assert_not_called()


def test_silo_failure_is_recorded_in_durable_attempt(monkeypatch) -> None:
    import healthcare_platform.ingestion.fhir as fhir_module

    events: list[str] = []
    monkeypatch.setattr(
        fhir_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(events),
    )
    store = MagicMock()
    store.put_raw.side_effect = fhir_module.psycopg.OperationalError("synthetic persistence outage")

    result = FhirIngestionService("unused", store, "test-source").ingest(PATIENT)

    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert "synthetic persistence outage" not in result["error"]
    assert "audit_failure_update" in events


def test_failure_to_persist_audit_status_is_returned_explicitly(monkeypatch) -> None:
    import healthcare_platform.ingestion.fhir as fhir_module

    events: list[str] = []
    connection_count = 0

    def connect(*_args, **_kwargs):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 2:
            return FakeConnection(
                events,
                fail_sql="ON CONFLICT (ingestion_id) DO UPDATE SET status='failed'",
            )
        return FakeConnection(events)

    monkeypatch.setattr(fhir_module.psycopg, "connect", connect)
    store = MagicMock()
    store.put_raw.side_effect = fhir_module.psycopg.OperationalError("synthetic persistence outage")

    result = FhirIngestionService("unused", store, "test-source").ingest(PATIENT)

    assert result["status"] == "audit_failed"
