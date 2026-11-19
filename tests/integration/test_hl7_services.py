"""Compose integration checks using synthetic HL7 v2 messages only."""

import hashlib
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from healthcare_platform.config import settings
from healthcare_platform.ingestion.hl7 import Hl7IngestionService
from healthcare_platform.storage.silo import SiloObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def require_hl7_services() -> None:
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    try:
        with psycopg.connect(_host_url(settings.database_url), connect_timeout=2) as connection:
            connection.execute("SELECT 1 FROM hl7_ingestion_attempts LIMIT 1")
        store._client.list_buckets()
        _wait_for_mllp_listener()
    except Exception as exc:
        reason = f"Local HL7/PostgreSQL/Silo services unavailable: {type(exc).__name__}"
        if os.getenv("REQUIRE_INTEGRATION_SERVICES", "").lower() == "true":
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)


def _host_url(value: str) -> str:
    return value.replace("minio:", "localhost:").replace("@postgres:", "@localhost:")


def _message(
    control_id: str,
    result: str = "4.2",
    sender: str = "SYNTH",
    facility: str = "SYNTH_FAC",
) -> bytes:
    return (
        f"MSH|^~\\&|{sender}|{facility}|PLATFORM|SYNTH|20250101120000||"
        f"ORU^R01|{control_id}|P|2.5.1\r"
        "PID|1||synthetic-patient^^^SYNTH^MR\r"
        "ORC|RE|synthetic-order|synthetic-filler\r"
        "OBR|1|synthetic-order|synthetic-filler|SYNTH^Synthetic Observation\r"
        f"OBX|1|NM|SYNTH^Synthetic Result||{result}|mg/dL|0-10||||F|||20250101120000\r"
    ).encode()


def _send(payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", settings.hl7_mllp_port), timeout=5) as client:
        client.settimeout(10)
        client.sendall(b"\x0b" + payload + b"\x1c\x0d")
        response = bytearray()
        while not response.endswith(b"\x1c\x0d"):
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    return bytes(response)


def _wait_for_mllp_listener() -> None:
    probe = _message(f"synthetic-readiness-{uuid4().hex}")
    for _ in range(60):
        try:
            ack = _send(probe)
            if b"MSA|AA|" in ack:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError("HL7 MLLP listener did not become ready")


def test_mllp_ack_storage_duplicate_and_conflicting_id() -> None:
    control_id = f"synthetic-{uuid4().hex}"
    first = _message(control_id)
    ack = _send(first)
    assert b"MSA|AA|" + control_id.encode() in ack

    with psycopg.connect(_host_url(settings.database_url)) as connection:
        row = connection.execute(
            """SELECT id,raw_object_key,raw_sha256,parsed_message FROM hl7_messages
            WHERE source_id=%s AND sending_application='SYNTH' AND sending_facility='SYNTH_FAC'
            AND message_control_id=%s""",
            (settings.hl7_source_id, control_id),
        ).fetchone()
    assert row is not None
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    stored = store._client.get_object(Bucket=settings.silo_bucket_raw, Key=row[1])["Body"].read()
    assert stored == first
    assert row[2].strip() == hashlib.sha256(first).hexdigest()
    assert row[3]["segments"][-1]["name"] == "OBX"

    duplicate = _send(first)
    assert b"MSA|AA|" + control_id.encode() in duplicate
    conflict = _send(_message(control_id, "5.1"))
    assert b"MSA|AR|" + control_id.encode() in conflict


def test_mllp_malformed_and_unsupported_messages_receive_error_acks() -> None:
    malformed_id = f"synthetic-malformed-{uuid4().hex}"
    malformed = (
        f"MSH|^~\\&|SYNTH|SYNTH_FAC|PLATFORM|SYNTH|20250101120000||ADT^A01|{malformed_id}|P|2.5.1\r"
    ).encode()
    assert b"MSA|AE|" + malformed_id.encode() in _send(malformed)

    unsupported_id = f"synthetic-unsupported-{uuid4().hex}"
    unsupported = (
        "MSH|^~\\&|SYNTH|SYNTH_FAC|PLATFORM|SYNTH|20250101120000||"
        f"ADT^A99|{unsupported_id}|P|2.5.1\r"
    ).encode()
    assert b"MSA|AR|" + unsupported_id.encode() in _send(unsupported)


def test_concurrent_same_message_is_idempotent() -> None:
    control_id = f"synthetic-concurrent-{uuid4().hex}"
    payload = _message(control_id)
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        store,
        settings.hl7_source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: service.ingest(payload)["outcome"], range(4)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("unchanged") == 3
    with psycopg.connect(_host_url(settings.database_url)) as connection:
        count = connection.execute(
            "SELECT count(*) FROM hl7_messages WHERE message_control_id=%s", (control_id,)
        ).fetchone()[0]
    assert count == 1


def test_same_control_id_from_different_senders_is_independent_concurrently() -> None:
    source_id = f"synthetic-sender-source-{uuid4().hex}"
    message_a = _message("CTL-001", sender="SENDER_A", facility="FACILITY_A")
    message_b = _message("CTL-001", sender="SENDER_B", facility="FACILITY_B")
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        store,
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.ingest, (message_a, message_b)))
    assert [result["outcome"] for result in results] == ["accepted", "accepted"]
    assert results[0]["message_id"] != results[1]["message_id"]

    with psycopg.connect(_host_url(settings.database_url)) as connection:
        messages = connection.execute(
            """SELECT sending_application,sending_facility,message_control_id,
            raw_object_key,raw_sha256 FROM hl7_messages WHERE source_id=%s
            AND message_control_id='CTL-001' ORDER BY sending_application""",
            (source_id,),
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in messages] == [
        ("SENDER_A", "FACILITY_A", "CTL-001"),
        ("SENDER_B", "FACILITY_B", "CTL-001"),
    ]
    for row, payload, result in zip(messages, (message_a, message_b), results, strict=True):
        stored = store._client.get_object(Bucket=settings.silo_bucket_raw, Key=row[3])[
            "Body"
        ].read()
        assert stored == payload
        assert row[4].strip() == hashlib.sha256(payload).hexdigest()
        attempt = service.status(UUID(result["attempt_id"]))
        assert attempt is not None and attempt["status"] == "accepted"
        assert attempt["message_id"] == result["message_id"]


def test_silo_failure_is_audited_with_retryable_original_bytes() -> None:
    class FailingStore:
        def ensure_bucket(self) -> None:
            raise RuntimeError("synthetic Silo outage")

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise AssertionError("put_raw_immutable should not run when bucket check fails")

    payload = _message(f"synthetic-silo-failure-{uuid4().hex}")
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        FailingStore(),
        settings.hl7_source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
    )
    result = service.ingest(payload)
    assert result["status"] == "failed"
    assert result["retryable"] is True
    status = service.status(UUID(result["attempt_id"]))
    assert status is not None and status["error_code"] == "dependency_failure"
    assert status["completed_at"] is None
    assert status["lease_expires_at"] is not None
    with psycopg.connect(_host_url(settings.database_url)) as connection:
        row = connection.execute(
            "SELECT raw_payload FROM hl7_ingestion_attempts WHERE id=%s",
            (result["attempt_id"],),
        ).fetchone()
    assert row[0] == payload


def test_retryable_failure_recovers_and_serializes_concurrent_reconcilers() -> None:
    control_id = f"synthetic-retry-{uuid4().hex}"
    source_id = f"synthetic-retry-source-{uuid4().hex}"
    payload = _message(control_id)

    class TransientStoreFailure:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise RuntimeError("synthetic transient Silo failure")

    database_url = _host_url(settings.database_url)
    failing_service = Hl7IngestionService(
        database_url,
        TransientStoreFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    first = failing_service.ingest(payload)
    assert first["status"] == "failed" and first["retryable"] is True
    first_status = failing_service.status(UUID(first["attempt_id"]))
    assert first_status is not None and first_status["retry_count"] == 0
    with psycopg.connect(database_url) as connection:
        original_payload = connection.execute(
            "SELECT raw_payload FROM hl7_ingestion_attempts WHERE id=%s",
            (first["attempt_id"],),
        ).fetchone()[0]
    assert original_payload == payload

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts
            SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=%s""",
            (first["attempt_id"],),
        )

    upload_started = threading.Event()
    allow_upload_failure = threading.Event()

    class BlockingTransientFailure:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            upload_started.set()
            if not allow_upload_failure.wait(timeout=5):
                raise TimeoutError("test upload gate timed out")
            raise RuntimeError("synthetic transient retry failure")

    worker_one = Hl7IngestionService(
        database_url,
        BlockingTransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    worker_two = Hl7IngestionService(
        database_url,
        TransientStoreFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_reconcile = pool.submit(worker_one.reconcile_stale_attempts, 10)
        assert upload_started.wait(timeout=5)
        with psycopg.connect(database_url) as connection:
            claimed = connection.execute(
                """SELECT status,retry_count,started_at,lease_expires_at > NOW()
                FROM hl7_ingestion_attempts WHERE id=%s""",
                (first["attempt_id"],),
            ).fetchone()
        assert claimed == (
            "processing",
            1,
            datetime.fromisoformat(first_status["started_at"]),
            True,
        )
        second_reconcile = pool.submit(worker_two.reconcile_stale_attempts, 10)
        assert second_reconcile.result(timeout=5) == 0
        allow_upload_failure.set()
        assert first_reconcile.result(timeout=5) == 1

    failed_retry = failing_service.status(UUID(first["attempt_id"]))
    assert failed_retry is not None
    assert failed_retry["status"] == "failed" and failed_retry["retryable"] is True
    assert failed_retry["retry_count"] == 1
    assert failed_retry["started_at"] == first_status["started_at"]
    with psycopg.connect(database_url) as connection:
        retained_payload = connection.execute(
            "SELECT raw_payload FROM hl7_ingestion_attempts WHERE id=%s",
            (first["attempt_id"],),
        ).fetchone()[0]
    assert retained_payload == payload
    assert failed_retry["lease_expires_at"] is not None
    assert failed_retry["retry_history"][0]["result_status"] == "failed"

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts
            SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=%s""",
            (first["attempt_id"],),
        )
    silo = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    recovery = Hl7IngestionService(
        database_url,
        silo,
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    assert recovery.reconcile_stale_attempts(10) == 1
    recovered = recovery.status(UUID(first["attempt_id"]))
    assert recovered is not None and recovered["status"] == "accepted"
    assert recovered["retry_count"] == 2
    assert recovered["started_at"] == first_status["started_at"]
    assert recovered["raw_object_key"] == first_status["raw_object_key"]
    assert recovered["completed_at"] is not None and recovered["lease_expires_at"] is None
    assert datetime.fromisoformat(recovered["completed_at"]) >= datetime.fromisoformat(
        recovered["started_at"]
    )
    assert [item["result_status"] for item in recovered["retry_history"]] == [
        "failed",
        "accepted",
    ]
    late_failure = recovery._finish_error(
        UUID(first["attempt_id"]),
        "failed",
        "late_failure",
        "late synthetic failure",
        True,
        expected_retry_count=0,
    )
    assert late_failure["status"] == "accepted"
    current_status = recovery.status(UUID(first["attempt_id"]))
    assert current_status is not None and current_status["status"] == "accepted"
    assert (
        silo._client.get_object(Bucket=settings.silo_bucket_raw, Key=recovered["raw_object_key"])[
            "Body"
        ].read()
        == payload
    )
    with psycopg.connect(database_url) as connection:
        message = connection.execute(
            """SELECT id,raw_sha256 FROM hl7_messages
            WHERE sending_application='SYNTH' AND sending_facility='SYNTH_FAC'
            AND message_control_id=%s""",
            (control_id,),
        ).fetchone()
    assert message is not None and message[1].strip() == hashlib.sha256(payload).hexdigest()


def test_retry_timestamps_duration_metrics_and_successful_recovery() -> None:
    source_id = f"synthetic-duration-source-{uuid4().hex}"
    payload = _message(f"synthetic-duration-{uuid4().hex}")

    class TransientFailure:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise RuntimeError("synthetic transient failure")

    database_url = _host_url(settings.database_url)
    failed_service = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        3,
    )
    first = failed_service.ingest(payload)
    attempt_id = UUID(first["attempt_id"])
    original = failed_service.status(attempt_id)
    assert original is not None and original["completed_at"] is None

    # Seed a controlled previous-generation duration and long recovery wait.
    with psycopg.connect(database_url) as connection:
        controlled_start = (
            connection.execute(
                """UPDATE hl7_ingestion_attempts SET processing_duration_ms=4000,
            started_at=clock_timestamp()-INTERVAL '1 hour',
            lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s
            RETURNING started_at""",
                (attempt_id,),
            )
            .fetchone()[0]
            .isoformat()
        )
    retry_one = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        3,
    )
    assert retry_one.reconcile_stale_attempts() == 1
    after_retry = retry_one.status(attempt_id)
    assert after_retry is not None
    assert after_retry["retry_count"] == 1
    assert after_retry["completed_at"] is None
    assert after_retry["processing_duration_ms"] >= 4000
    retry_record = after_retry["retry_history"][0]
    assert retry_record["completed_at"] is not None
    assert retry_record["processing_duration_ms"] >= 0

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts SET
            lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s""",
            (attempt_id,),
        )
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    recovery = Hl7IngestionService(
        database_url,
        store,
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        3,
    )
    assert recovery.reconcile_stale_attempts() == 1
    recovered = recovery.status(attempt_id)
    assert recovered is not None and recovered["status"] == "accepted"
    assert recovered["completed_at"] is not None
    assert recovered["started_at"] == controlled_start
    metrics = recovery.metrics(source_id)
    assert 4.0 <= metrics["avg_processing_duration_seconds"] < 10.0
    assert metrics["avg_end_to_end_elapsed_seconds"] >= 3599.0


def test_retry_exhaustion_closes_open_history_and_fences_late_worker() -> None:
    source_id = f"synthetic-exhaustion-source-{uuid4().hex}"
    payload = _message(f"synthetic-exhaustion-{uuid4().hex}")
    database_url = _host_url(settings.database_url)

    class TransientFailure:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise RuntimeError("synthetic transient failure")

    initial = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    result = initial.ingest(payload)
    attempt_id = UUID(result["attempt_id"])
    before_claim = initial.status(attempt_id)
    assert before_claim is not None
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts SET
            lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s""",
            (attempt_id,),
        )

    class SimulatedWorkerCrash(BaseException):
        pass

    class CrashingStore:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise SimulatedWorkerCrash()

    final_claim = Hl7IngestionService(
        database_url,
        CrashingStore(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    with pytest.raises(SimulatedWorkerCrash):
        final_claim.reconcile_stale_attempts()
    processing = final_claim.status(attempt_id)
    assert processing is not None and processing["status"] == "processing"
    assert processing["retry_count"] == 1
    assert processing["completed_at"] is None
    assert processing["active_processing_started_at"] is not None
    assert processing["retry_history"][-1]["completed_at"] is None
    assert processing["started_at"] == before_claim["started_at"]
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts SET
            lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s""",
            (attempt_id,),
        )

    terminal = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    assert terminal.reconcile_stale_attempts() == 0
    status = terminal.status(attempt_id)
    assert status is not None and status["status"] == "failed"
    assert status["retryable"] is False and status["completed_at"] is not None
    assert status["error_code"] == "reconciliation_exhausted"
    assert status["retry_history"][-1]["completed_at"] is not None
    assert status["retry_history"][-1]["result_status"] == "failed"
    assert status["retry_history"][-1]["result_error_code"] == "reconciliation_exhausted"
    assert all(item["completed_at"] is not None for item in status["retry_history"])
    late = terminal._finish_error(
        attempt_id,
        "failed",
        "late_failure",
        "late synthetic worker failure",
        True,
        expected_retry_count=1,
    )
    assert late["error_code"] == "reconciliation_exhausted"
    assert terminal.status(attempt_id)["error_code"] == "reconciliation_exhausted"


def test_concurrent_exhaustion_cannot_be_overwritten_by_stale_worker() -> None:
    source_id = f"synthetic-stale-worker-source-{uuid4().hex}"
    payload = _message(f"synthetic-stale-worker-{uuid4().hex}")
    database_url = _host_url(settings.database_url)
    gate_entered = threading.Event()
    release_upload = threading.Event()

    class TransientFailure:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            raise RuntimeError("synthetic transient failure")

    initial = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    result = initial.ingest(payload)
    attempt_id = UUID(result["attempt_id"])
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """UPDATE hl7_ingestion_attempts SET
            lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s""",
            (attempt_id,),
        )

    class DelayedStore:
        def ensure_bucket(self) -> None:
            pass

        def put_raw_immutable(self, *args, **kwargs) -> None:
            gate_entered.set()
            if not release_upload.wait(timeout=10):
                raise TimeoutError("synthetic upload gate timed out")

    worker = Hl7IngestionService(
        database_url,
        DelayedStore(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    exhaust = Hl7IngestionService(
        database_url,
        TransientFailure(),
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        1,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.reconcile_stale_attempts)
        assert gate_entered.wait(timeout=5)
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """UPDATE hl7_ingestion_attempts SET
                lease_expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=%s""",
                (attempt_id,),
            )
        assert exhaust.reconcile_stale_attempts() == 0
        release_upload.set()
        assert pending.result(timeout=10) == 1

    status = exhaust.status(attempt_id)
    assert status is not None and status["status"] == "failed"
    assert status["error_code"] == "reconciliation_exhausted"
    assert status["retry_history"][-1]["result_error_code"] == "reconciliation_exhausted"


def test_nonretryable_failed_attempt_is_not_reconciled() -> None:
    payload = _message(f"synthetic-nonretryable-{uuid4().hex}").replace(
        b"synthetic-patient^^^SYNTH^MR", b""
    )
    silo = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        silo,
        f"synthetic-nonretryable-source-{uuid4().hex}",
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    result = service.ingest(payload)
    status = service.status(UUID(result["attempt_id"]))
    assert status is not None and status["status"] == "failed"
    assert status["retryable"] is False and status["retry_count"] == 0
    assert status["completed_at"] is not None
    assert service.reconcile_stale_attempts(10) == 0
    after = service.status(UUID(result["attempt_id"]))
    assert after is not None and after["status"] == "failed" and after["retry_count"] == 0


def test_stale_processing_attempt_reconciles_without_new_attempt() -> None:
    control_id = f"synthetic-recovery-{uuid4().hex}"
    payload = _message(control_id)
    digest = hashlib.sha256(payload).hexdigest()
    attempt_id = uuid4()
    source_id = f"synthetic-recovery-{uuid4().hex}"
    object_key = f"hl7-v2/SYNTH-SYNTH_FAC/2025/01/01/{digest}.hl7"
    with psycopg.connect(_host_url(settings.database_url)) as connection:
        started_at = connection.execute(
            """INSERT INTO hl7_ingestion_attempts
            (id,source_id,sending_application,sending_facility,message_control_id,message_type,
             trigger_event,hl7_version,raw_sha256,raw_object_key,raw_payload,status,outcome,
             started_at,updated_at,lease_expires_at,active_processing_started_at)
            VALUES (%s,%s,'SYNTH','SYNTH_FAC',%s,'ORU','R01','2.5.1',%s,%s,%s,
                    'processing','processing',NOW()-INTERVAL '10 minutes',
                    NOW()-INTERVAL '9 minutes',NOW()-INTERVAL '6 minutes',
                    NOW()-INTERVAL '10 minutes') RETURNING started_at""",
            (attempt_id, source_id, control_id, digest, object_key, payload),
        ).fetchone()[0]
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        store,
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
    )
    assert service.reconcile_stale_attempts(limit=10) == 1
    status = service.status(attempt_id)
    assert status is not None and status["status"] == "accepted"
    assert status["retry_count"] == 1
    assert 59_000 <= status["processing_duration_ms"] < 61_000
    assert len(status["retry_history"]) == 1
    assert status["retry_history"][0]["result_status"] == "accepted"
    assert status["started_at"] == datetime.fromisoformat(started_at.isoformat()).isoformat()
    assert store._client.head_object(Bucket=settings.silo_bucket_raw, Key=object_key)


def test_stale_attempt_recovery_is_bounded() -> None:
    attempt_id = uuid4()
    failed_attempt_id = uuid4()
    source_id = f"synthetic-exhausted-{uuid4().hex}"
    payload = _message(f"synthetic-exhausted-{uuid4().hex}")
    failed_payload = _message(f"synthetic-exhausted-failed-{uuid4().hex}")
    with psycopg.connect(_host_url(settings.database_url)) as connection:
        connection.execute(
            """INSERT INTO hl7_ingestion_attempts
            (id,source_id,raw_sha256,raw_object_key,raw_payload,status,outcome,retry_count,
             started_at,lease_expires_at)
            VALUES (%s,%s,%s,%s,%s,'processing','processing',%s,NOW()-INTERVAL '10 minutes',
                    NOW()-INTERVAL '6 minutes')""",
            (
                attempt_id,
                source_id,
                hashlib.sha256(payload).hexdigest(),
                "hl7-v2/unknown/undated/not-uploaded.hl7",
                payload,
                settings.hl7_reconciliation_max_retries,
            ),
        )
        connection.execute(
            """INSERT INTO hl7_ingestion_attempts
            (id,source_id,raw_sha256,raw_object_key,raw_payload,status,outcome,retryable,
             retry_count,error_code,sanitized_error,started_at,completed_at,lease_expires_at)
            VALUES (%s,%s,%s,%s,%s,'failed','failed',TRUE,%s,'dependency_failure',
                    'synthetic transient failure',NOW()-INTERVAL '10 minutes',
                    NOW()-INTERVAL '6 minutes',NOW()-INTERVAL '6 minutes')""",
            (
                failed_attempt_id,
                source_id,
                hashlib.sha256(failed_payload).hexdigest(),
                "hl7-v2/unknown/undated/retryable-failed.hl7",
                failed_payload,
                settings.hl7_reconciliation_max_retries,
            ),
        )
    store = SiloObjectStore(
        _host_url(settings.silo_endpoint),
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        _host_url(settings.database_url),
        store,
        source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    assert service.reconcile_stale_attempts(limit=10) == 0
    status = service.status(attempt_id)
    assert status is not None and status["error_code"] == "reconciliation_exhausted"
    failed_status = service.status(failed_attempt_id)
    assert failed_status is not None
    assert failed_status["error_code"] == "reconciliation_exhausted"
    with psycopg.connect(_host_url(settings.database_url)) as connection:
        raw_payload = connection.execute(
            "SELECT raw_payload,retryable,retry_count FROM hl7_ingestion_attempts WHERE id=%s",
            (attempt_id,),
        ).fetchone()
    assert raw_payload == (payload, False, settings.hl7_reconciliation_max_retries)
