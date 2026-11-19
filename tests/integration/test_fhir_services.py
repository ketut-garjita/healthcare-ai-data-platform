"""Live integration checks; require the local Compose services to be running."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import psycopg
import pytest

from healthcare_platform.config import settings
from healthcare_platform.ingestion.fhir import FhirIngestionService
from healthcare_platform.integrations.fhir.client import FhirClient
from healthcare_platform.storage.silo import SiloObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def require_local_services() -> None:
    client = FhirClient(host_url(settings.fhir_base_url), settings.fhir_timeout_seconds)
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    try:
        client.verify_r4()
        with psycopg.connect(host_url(settings.database_url), connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        store._client.list_buckets()
    except Exception as exc:
        reason = f"Local FHIR/PostgreSQL/Silo services are unavailable: {type(exc).__name__}"
        if os.getenv("REQUIRE_INTEGRATION_SERVICES", "").lower() == "true":
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)
    finally:
        client.close()


def host_url(value: str) -> str:
    return (
        value.replace("http://fhir:", "http://localhost:")
        .replace("minio:", "localhost:")
        .replace("@postgres:", "@localhost:")
    )


@pytest.mark.parametrize(
    "resource",
    [
        {"resourceType": "Patient", "active": True, "name": [{"text": "Synthetic Patient"}]},
        {
            "resourceType": "Practitioner",
            "active": True,
            "name": [{"text": "Synthetic Practitioner"}],
        },
        {
            "resourceType": "Encounter",
            "status": "finished",
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        },
        {
            "resourceType": "Observation",
            "status": "final",
            "code": {"text": "Synthetic observation"},
            "valueString": "synthetic result",
        },
    ],
)
def test_hapi_supported_resource_create_search_read_update(resource: dict) -> None:
    client = FhirClient(host_url(settings.fhir_base_url), settings.fhir_timeout_seconds)
    try:
        assert client.verify_r4()["fhirVersion"] == "4.0.1"
        created = client.create(resource)
        resource_type, resource_id = created["resourceType"], created["id"]
        found = client.search(resource_type, {"_id": resource_id})
        assert any(item.get("resource", {}).get("id") == resource_id for item in found["entry"])
        assert client.read(resource_type, resource_id)["id"] == resource_id
        changed = deepcopy(created)
        changed.setdefault("extension", []).append(
            {
                "url": "https://example.invalid/fhir/StructureDefinition/synthetic-test-marker",
                "valueString": str(uuid4()),
            }
        )
        updated = client.update(changed)
        assert updated["id"] == resource_id
        assert updated.get("meta", {}).get("versionId") != created.get("meta", {}).get("versionId")
    finally:
        client.close()


def test_ingestion_persists_raw_json_jsonb_and_version_history() -> None:
    suffix = uuid4().hex
    resource = {
        "resourceType": "Patient",
        "id": f"synthetic-ingest-{suffix}",
        "active": True,
        "meta": {"lastUpdated": "2025-01-01T00:00:00Z", "versionId": "opaque-a"},
    }
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-fhir")
    first = service.ingest(resource)
    assert first["status"] == "ingested" and first["outcome"] == "created"
    assert store._client.head_object(Bucket=settings.silo_bucket_raw, Key=first["raw_object_key"])
    assert service.ingest(resource)["status"] == "unchanged"
    changed = {
        **resource,
        "active": False,
        "meta": {"versionId": "opaque-b", "lastUpdated": "2025-02-01T00:00:00Z"},
    }
    second = service.ingest(changed)
    assert second["status"] == "ingested" and second["outcome"] == "updated"
    assert second["content_hash"] != first["content_hash"]
    first_status = service.status(UUID(first["ingestion_id"]))
    assert first_status is not None and first_status["status"] == "ingested"
    with psycopg.connect(host_url(settings.database_url)) as connection:
        current = connection.execute(
            "SELECT payload, content_hash FROM fhir_resources WHERE source=%s AND resource_id=%s",
            ("integration-fhir", resource["id"]),
        ).fetchone()
        versions = connection.execute(
            "SELECT count(*) FROM fhir_resource_versions WHERE source=%s AND resource_id=%s",
            ("integration-fhir", resource["id"]),
        ).fetchone()
    assert current is not None and current[0]["active"] is False
    assert current[1].strip() == second["content_hash"]
    assert versions is not None and versions[0] == 2


def test_late_failure_cannot_overwrite_a_terminal_ingestion_attempt() -> None:
    suffix = uuid4().hex
    resource = {"resourceType": "Patient", "id": f"synthetic-terminal-{suffix}"}
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-terminal")
    result = service.ingest(resource)

    late_result = service._save_failure(
        UUID(result["ingestion_id"]),
        "Patient",
        resource["id"],
        result["content_hash"],
        result["raw_object_key"],
        resource,
        RuntimeError("late synthetic failure"),
    )
    status = service.status(UUID(result["ingestion_id"]))

    assert status is not None
    assert late_result["status"] == status["status"] == "ingested"
    assert late_result["outcome"] == status["outcome"] == "created"


def test_timestamped_resource_promotes_over_initial_unordered_content() -> None:
    suffix = uuid4().hex
    resource_id = f"synthetic-null-timestamp-{suffix}"
    initial = {
        "resourceType": "Patient",
        "id": resource_id,
        "active": True,
        "meta": {"versionId": "opaque-newer-looking"},
    }
    timestamped = {
        **initial,
        "active": False,
        "meta": {
            "versionId": "opaque-older-looking",
            "lastUpdated": "2025-09-01T00:00:00Z",
        },
    }
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(
        host_url(settings.database_url), store, "integration-null-timestamp"
    )

    first = service.ingest(initial)
    assert first["status"] == "ingested" and first["outcome"] == "created"
    promoted = service.ingest(timestamped)
    assert promoted["status"] == "ingested" and promoted["outcome"] == "updated"
    assert service.ingest(timestamped)["outcome"] == "unchanged"

    with psycopg.connect(host_url(settings.database_url)) as connection:
        current = connection.execute(
            """SELECT payload, last_updated, content_hash FROM fhir_resources
            WHERE source=%s AND resource_type='Patient' AND resource_id=%s""",
            ("integration-null-timestamp", resource_id),
        ).fetchone()
        versions = connection.execute(
            """SELECT payload FROM fhir_resource_versions
            WHERE source=%s AND resource_type='Patient' AND resource_id=%s
            ORDER BY observed_at, content_hash""",
            ("integration-null-timestamp", resource_id),
        ).fetchall()

    assert current is not None
    assert current[0]["active"] is False
    assert current[1].isoformat() == "2025-09-01T00:00:00+00:00"
    assert current[2].strip() == promoted["content_hash"]
    assert len(versions) == 2
    assert any(version[0]["active"] is True for version in versions)


def test_older_equal_conflicting_and_unordered_versions_never_replace_current() -> None:
    suffix = uuid4().hex
    resource_id = f"synthetic-order-{suffix}"
    base = {
        "resourceType": "Patient",
        "id": resource_id,
        "active": True,
        "meta": {"lastUpdated": "2025-03-01T00:00:00Z", "versionId": "z"},
    }
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-order")
    assert service.ingest(base)["outcome"] == "created"
    newer = {
        **base,
        "active": False,
        "meta": {"lastUpdated": "2025-04-01T00:00:00Z", "versionId": "a"},
    }
    assert service.ingest(newer)["outcome"] == "updated"
    older = {
        **base,
        "active": True,
        "meta": {"lastUpdated": "2025-02-01T00:00:00Z", "versionId": "zz"},
    }
    assert service.ingest(older)["outcome"] == "historical_only"
    conflict = {
        **newer,
        "active": True,
        "meta": {"lastUpdated": "2025-04-01T00:00:00Z", "versionId": "0"},
    }
    conflict_result = service.ingest(conflict)
    assert conflict_result["status"] == "ingested"
    assert conflict_result["outcome"] == "conflict"
    unordered = {**base, "active": True, "meta": {"versionId": "opaque"}}
    assert service.ingest(unordered)["outcome"] == "historical_only"
    with psycopg.connect(host_url(settings.database_url)) as connection:
        current = connection.execute(
            "SELECT payload FROM fhir_resources WHERE source=%s AND resource_id=%s",
            ("integration-order", resource_id),
        ).fetchone()
        version_count = connection.execute(
            "SELECT count(*) FROM fhir_resource_versions WHERE source=%s AND resource_id=%s",
            ("integration-order", resource_id),
        ).fetchone()[0]
        outcomes = connection.execute(
            "SELECT outcome FROM fhir_ingestion_attempts WHERE source=%s AND resource_id=%s",
            ("integration-order", resource_id),
        ).fetchall()
    assert current is not None and current[0]["active"] is False
    assert version_count == 5
    assert {row[0] for row in outcomes} >= {"created", "updated", "historical_only", "conflict"}


def test_concurrent_versions_leave_newest_timestamp_current() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    suffix = uuid4().hex
    resource_id = f"synthetic-concurrent-{suffix}"
    barrier = Barrier(2)
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-concurrent")
    versions = [
        {
            "resourceType": "Patient",
            "id": resource_id,
            "active": index == 2,
            "meta": {"lastUpdated": f"2025-05-0{index}T00:00:00Z", "versionId": str(3 - index)},
        }
        for index in (1, 2)
    ]

    def ingest(resource: dict) -> dict:
        barrier.wait(timeout=5)
        return service.ingest(resource)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, versions))
    assert all(result["status"] == "ingested" for result in results)
    with psycopg.connect(host_url(settings.database_url)) as connection:
        current = connection.execute(
            "SELECT payload FROM fhir_resources WHERE source=%s AND resource_id=%s",
            ("integration-concurrent", resource_id),
        ).fetchone()
        version_count = connection.execute(
            "SELECT count(*) FROM fhir_resource_versions WHERE source=%s AND resource_id=%s",
            ("integration-concurrent", resource_id),
        ).fetchone()[0]
    assert current is not None and current[0]["meta"]["lastUpdated"] == "2025-05-02T00:00:00Z"
    assert version_count == 2


def test_delayed_silo_upload_does_not_hold_resource_advisory_lock() -> None:
    suffix = uuid4().hex
    resource_id = f"synthetic-delayed-silo-{suffix}"
    source = "integration-delayed-silo"
    resource = {
        "resourceType": "Patient",
        "id": resource_id,
        "meta": {"lastUpdated": "2025-05-03T00:00:00Z"},
    }
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    original_put_raw = store.put_raw
    upload_started = threading.Event()
    delay_seconds = 1.0

    def delayed_put_raw(key, payload, digest):
        upload_started.set()
        time.sleep(delay_seconds)
        original_put_raw(key, payload, digest)

    store.put_raw = delayed_put_raw
    service = FhirIngestionService(host_url(settings.database_url), store, source)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.ingest, resource)
        assert upload_started.wait(timeout=5)
        lock_key = f'["{source}","Patient","{resource_id}"]'
        probe_started = time.perf_counter()
        with psycopg.connect(host_url(settings.database_url)) as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            ).fetchone()[0]
        probe_seconds = time.perf_counter() - probe_started
        assert acquired is True
        assert probe_seconds < delay_seconds
        assert future.result(timeout=10)["outcome"] == "created"


def test_reconciler_reuses_silo_object_after_postgres_failure(monkeypatch) -> None:
    import psycopg

    from healthcare_platform.ingestion.fhir import canonical_payload

    suffix = uuid4().hex
    resource = {
        "resourceType": "Patient",
        "id": f"synthetic-reconcile-{suffix}",
        "active": True,
        "meta": {"lastUpdated": "2025-06-01T00:00:00Z"},
    }
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-reconcile")
    original_process = service._process_attempt

    def upload_then_fail(ingestion_id, resource_type, resource_id, digest, key, payload):
        raise psycopg.OperationalError("injected persistence interruption")

    monkeypatch.setattr(service, "_process_attempt", upload_then_fail)
    failed = service.ingest(resource)
    assert failed["status"] == "failed" and failed["retryable"] is True
    failed_status = service.status(UUID(failed["ingestion_id"]))
    assert failed_status is not None
    key = failed_status["raw_object_key"]
    first_body = store._client.get_object(Bucket=settings.silo_bucket_raw, Key=key)["Body"].read()
    assert first_body == canonical_payload(resource)

    with psycopg.connect(host_url(settings.database_url)) as connection:
        connection.execute(
            "UPDATE fhir_ingestion_attempts SET lease_expires_at=NOW()-INTERVAL '1 second' "
            "WHERE ingestion_id=%s",
            (UUID(failed["ingestion_id"]),),
        )
    monkeypatch.setattr(service, "_process_attempt", original_process)
    results = service.reconcile_stale_attempts(limit=10)

    assert {
        "ingestion_id": failed["ingestion_id"],
        "status": "ingested",
        "outcome": "created",
    } in results
    status = service.status(UUID(failed["ingestion_id"]))
    assert status is not None and status["status"] == "ingested"
    assert status["retry_count"] == 1 and status["silo_uploaded"] is True
    second_body = store._client.get_object(Bucket=settings.silo_bucket_raw, Key=key)["Body"].read()
    assert second_body == first_body


def test_invalid_resource_failure_is_audited() -> None:
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(host_url(settings.database_url), store, "integration-fhir")
    result = service.ingest({"resourceType": "UnsupportedSyntheticType", "id": "safe-test-id"})
    assert result["status"] == "failed"
    attempt = service.status(UUID(result["ingestion_id"]))
    assert attempt is not None and attempt["status"] == "failed"
    assert attempt["error_category"] == "FhirError"


def test_two_reconcilers_do_not_claim_the_same_stale_attempt() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from healthcare_platform.ingestion.fhir import content_hash

    resource = {
        "resourceType": "Patient",
        "id": f"synthetic-reconcile-race-{uuid4().hex}",
        "active": True,
        "meta": {"lastUpdated": "2025-08-01T00:00:00Z"},
    }
    resource_type, resource_id = "Patient", resource["id"]
    digest = content_hash(resource)
    object_key = f"fhir/{resource_type}/{resource_id}/{digest}.json"
    ingestion_id = uuid4()
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(
        host_url(settings.database_url), store, "integration-reconcile-race"
    )
    service._create_attempt(ingestion_id, resource_type, resource_id, digest, object_key, resource)
    with psycopg.connect(host_url(settings.database_url)) as connection:
        connection.execute(
            "UPDATE fhir_ingestion_attempts SET lease_expires_at=NOW()-INTERVAL '1 second' "
            "WHERE ingestion_id=%s",
            (ingestion_id,),
        )
    barrier = Barrier(2)

    def reconcile() -> list[dict[str, str]]:
        barrier.wait(timeout=5)
        return service.reconcile_stale_attempts(limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reconcile(), range(2)))
    claimed = [item for result in results for item in result]
    assert claimed == [
        {"ingestion_id": str(ingestion_id), "status": "ingested", "outcome": "created"}
    ]


def test_reconciler_marks_attempt_failed_after_retry_limit() -> None:
    from healthcare_platform.ingestion.fhir import content_hash

    resource = {
        "resourceType": "Patient",
        "id": f"synthetic-reconcile-limit-{uuid4().hex}",
        "active": True,
    }
    resource_type, resource_id = "Patient", resource["id"]
    digest = content_hash(resource)
    object_key = f"fhir/{resource_type}/{resource_id}/{digest}.json"
    ingestion_id = uuid4()
    store = SiloObjectStore(
        host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(
        host_url(settings.database_url), store, "integration-reconcile-limit"
    )
    service._create_attempt(ingestion_id, resource_type, resource_id, digest, object_key, resource)
    with psycopg.connect(host_url(settings.database_url)) as connection:
        connection.execute(
            """UPDATE fhir_ingestion_attempts
            SET status='failed', retryable=TRUE, retry_count=1,
                lease_expires_at=NOW()-INTERVAL '1 second'
            WHERE ingestion_id=%s""",
            (ingestion_id,),
        )

    assert service.reconcile_stale_attempts(limit=1, max_retries=1) == []
    status = service.status(ingestion_id)
    assert status is not None
    assert status["status"] == "failed"
    assert status["error_category"] == "RetryLimit"
