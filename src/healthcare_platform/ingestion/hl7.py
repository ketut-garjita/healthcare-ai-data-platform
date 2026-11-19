"""Durable HL7 v2 ingestion with immutable raw-message capture."""

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from healthcare_platform.integrations.hl7.models import (
    Hl7MessageError,
    Hl7Metadata,
    ParsedHl7Message,
)
from healthcare_platform.integrations.hl7.parser import extract_msh_metadata, parse_hl7_message
from healthcare_platform.storage.silo import SiloObjectStore

logger = logging.getLogger(__name__)
_SAFE_KEY_PART = re.compile(r"[^A-Za-z0-9._-]+")


def hl7_content_hash(payload: bytes) -> str:
    """Hash the exact original message bytes, including ER7 delimiters/encoding."""
    return hashlib.sha256(payload).hexdigest()


def _safe_key_part(value: str | None) -> str:
    safe = _SAFE_KEY_PART.sub("_", (value or "unknown")).strip("._-")
    return (safe or "unknown")[:64]


class Hl7IngestionService:
    def __init__(
        self,
        database_url: str,
        object_store: SiloObjectStore,
        source_id: str = "local-hl7-mllp",
        supported_versions: tuple[str, ...] = ("2.5.1",),
        encoding: str = "utf-8",
        max_reconciliation_retries: int = 3,
    ) -> None:
        self.database_url = database_url
        self.object_store = object_store
        self.source_id = source_id
        self.supported_versions = supported_versions
        self.encoding = encoding
        self.max_reconciliation_retries = max_reconciliation_retries

    def ingest(self, payload: bytes) -> dict[str, object]:
        attempt_id = uuid4()
        digest = hl7_content_hash(payload)
        metadata = None
        try:
            metadata = extract_msh_metadata(payload, self.encoding)
        except Hl7MessageError:
            pass
        # Use the source timestamp when valid so redelivery on another day maps to
        # the same immutable object key. Undated/malformed messages use a stable bucket.
        source_day = None
        timestamp = metadata.message_timestamp if metadata else None
        if timestamp and re.match(r"^\d{8}", timestamp):
            try:
                source_day = datetime.strptime(timestamp[:8], "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                source_day = None
        sender = _safe_key_part(
            "-".join(filter(None, (metadata.sending_application, metadata.sending_facility)))
            if metadata
            else None
        )
        day_path = source_day.strftime("%Y/%m/%d") if source_day else "undated"
        object_key = f"hl7-v2/{sender}/{day_path}/{digest}.hl7"

        try:
            self._create_attempt(attempt_id, payload, digest, object_key, metadata)
        except Exception as exc:
            logger.error(
                "Could not persist HL7 ingestion attempt",
                extra={"error_type": type(exc).__name__},
            )
            return self._response(
                attempt_id, "failed", "audit_failed", "audit_persistence_failed", False
            )

        try:
            # Content-addressed upload occurs outside the transaction used for identity locking.
            self.object_store.ensure_bucket()
            self.object_store.put_raw_immutable(
                object_key, payload, digest, content_type="application/hl7-v2"
            )
            with psycopg.connect(self.database_url) as connection:
                self._checkpoint_processing_duration(connection, attempt_id, 0)
                connection.execute(
                    """UPDATE hl7_ingestion_attempts SET silo_uploaded=TRUE,
                    lease_expires_at=NOW()+INTERVAL '5 minutes',updated_at=NOW()
                    WHERE id=%s AND status='processing' AND retry_count=0""",
                    (attempt_id,),
                )
            try:
                parsed = parse_hl7_message(
                    payload,
                    supported_versions=self.supported_versions,
                    encoding=self.encoding,
                )
            except Hl7MessageError as exc:
                terminal = "rejected" if exc.ack_code == "AR" else "failed"
                return self._finish_error(attempt_id, terminal, exc.code, str(exc), False)
            return self._promote(attempt_id, digest, object_key, parsed, expected_retry_count=0)
        except Exception as exc:
            logger.error(
                "HL7 ingestion failed after attempt was recorded",
                extra={"error_type": type(exc).__name__},
            )
            return self._finish_error(
                attempt_id,
                "failed",
                "dependency_failure",
                "HL7 message persistence failed; retry may be attempted",
                True,
            )

    def _create_attempt(
        self,
        attempt_id: UUID,
        payload: bytes,
        digest: str,
        object_key: str,
        metadata: Hl7Metadata | None,
    ) -> None:
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO hl7_ingestion_attempts
                (id, source_id, sending_application, sending_facility, message_control_id,
                 message_type, trigger_event, hl7_version, raw_sha256, raw_object_key,
                 raw_payload, status, outcome, lease_expires_at, active_processing_started_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'processing','processing',
                        NOW()+INTERVAL '5 minutes',clock_timestamp())""",
                (
                    attempt_id,
                    self.source_id,
                    metadata.sending_application if metadata else None,
                    metadata.sending_facility if metadata else None,
                    metadata.message_control_id if metadata else None,
                    metadata.message_code if metadata else None,
                    metadata.trigger_event if metadata else None,
                    metadata.version if metadata else None,
                    digest,
                    object_key,
                    payload,
                ),
            )

    def _promote(
        self,
        attempt_id: UUID,
        digest: str,
        object_key: str,
        parsed: ParsedHl7Message,
        *,
        expected_retry_count: int,
    ) -> dict[str, object]:
        meta = parsed.metadata
        identity = (
            self.source_id,
            meta.sending_application or "",
            meta.sending_facility or "",
            meta.message_control_id,
        )
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (json.dumps(identity, separators=(",", ":")),),
            )
            attempt = connection.execute(
                "SELECT status,retry_count FROM hl7_ingestion_attempts WHERE id=%s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt[0] != "processing" or attempt[1] != expected_retry_count:
                return self._attempt_response(connection, attempt_id)
            existing = connection.execute(
                """SELECT id, raw_sha256 FROM hl7_messages WHERE source_id=%s
                AND sending_application=%s AND sending_facility=%s AND message_control_id=%s
                FOR UPDATE""",
                identity,
            ).fetchone()
            if existing:
                message_id = existing[0]
                same = existing[1].strip() == digest
                outcome = "duplicate" if same else "conflict"
                connection.execute(
                    """INSERT INTO hl7_message_versions
                    (message_id, raw_object_key, raw_sha256, parsed_message, outcome)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT (message_id,raw_sha256) DO NOTHING""",
                    (message_id, object_key, digest, Jsonb(self._parsed_json(parsed)), outcome),
                )
                status = "unchanged" if same else "conflict"
            else:
                message_id = uuid4()
                connection.execute(
                    """INSERT INTO hl7_messages
                    (id,source_id,sending_application,sending_facility,message_control_id,
                     message_type,trigger_event,hl7_version,message_timestamp,raw_object_key,
                     raw_sha256,parsed_message,lifecycle_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'accepted')""",
                    (
                        message_id,
                        *identity,
                        meta.message_code,
                        meta.trigger_event,
                        meta.version,
                        meta.message_timestamp,
                        object_key,
                        digest,
                        Jsonb(self._parsed_json(parsed)),
                    ),
                )
                connection.execute(
                    """INSERT INTO hl7_message_versions
                    (message_id,raw_object_key,raw_sha256,parsed_message,outcome)
                    VALUES (%s,%s,%s,%s,'accepted')""",
                    (message_id, object_key, digest, Jsonb(self._parsed_json(parsed))),
                )
                status = "accepted"
            self._close_processing_generation(
                connection, attempt_id, expected_retry_count, result_status=status
            )
            connection.execute(
                """UPDATE hl7_ingestion_attempts SET status=%s,outcome=%s,message_id=%s,
                raw_payload=NULL,retryable=FALSE,lease_expires_at=NULL,
                error_code=NULL,sanitized_error=NULL,
                completed_at=clock_timestamp(),updated_at=clock_timestamp(),
                active_processing_started_at=NULL
                WHERE id=%s AND status='processing' AND retry_count=%s""",
                (status, status, message_id, attempt_id, expected_retry_count),
            )
        return self._response(attempt_id, status, status, None, False, str(message_id))

    @staticmethod
    def _attempt_response(
        connection: psycopg.Connection[Any], attempt_id: UUID
    ) -> dict[str, object]:
        row = connection.execute(
            """SELECT status,outcome,error_code,retryable,message_id
            FROM hl7_ingestion_attempts WHERE id=%s""",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return Hl7IngestionService._response(
                attempt_id, "failed", "failed", "attempt_not_found", False
            )
        return Hl7IngestionService._response(
            attempt_id, row[0], row[1], row[2], row[3], str(row[4]) if row[4] else None
        )

    @staticmethod
    def _finish_retry_record(
        connection: psycopg.Connection[Any],
        attempt_id: UUID,
        retry_number: int,
        result_status: str,
        result_error_code: str | None = None,
    ) -> None:
        if retry_number == 0:
            return
        connection.execute(
            """UPDATE hl7_ingestion_attempt_retries
            SET completed_at=clock_timestamp(),result_status=%s,result_error_code=%s
            WHERE attempt_id=%s AND retry_number=%s AND completed_at IS NULL""",
            (result_status, result_error_code, attempt_id, retry_number),
        )

    @staticmethod
    def _close_processing_generation(
        connection: psycopg.Connection[Any],
        attempt_id: UUID,
        retry_number: int,
        *,
        result_status: str,
        result_error_code: str | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        """Accumulate active time for one generation and close its retry ledger row."""
        row = connection.execute(
            """SELECT active_processing_started_at FROM hl7_ingestion_attempts
            WHERE id=%s FOR UPDATE""",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return
        started_at = row[0]
        if started_at is not None:
            if ended_at is None:
                elapsed = connection.execute(
                    """SELECT GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (clock_timestamp() - %s)) * 1000))::bigint""",
                    (started_at,),
                ).fetchone()[0]
            else:
                elapsed = connection.execute(
                    """SELECT GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (%s - %s)) * 1000))::bigint""",
                    (ended_at, started_at),
                ).fetchone()[0]
            connection.execute(
                """UPDATE hl7_ingestion_attempts SET
                processing_duration_ms=processing_duration_ms+%s,
                active_processing_started_at=NULL WHERE id=%s""",
                (elapsed, attempt_id),
            )
            if retry_number > 0:
                connection.execute(
                    """UPDATE hl7_ingestion_attempt_retries
                    SET processing_duration_ms=processing_duration_ms+%s
                    WHERE attempt_id=%s AND retry_number=%s AND completed_at IS NULL""",
                    (elapsed, attempt_id, retry_number),
                )
        Hl7IngestionService._finish_retry_record(
            connection, attempt_id, retry_number, result_status, result_error_code
        )

    @staticmethod
    def _checkpoint_processing_duration(
        connection: psycopg.Connection[Any], attempt_id: UUID, retry_number: int
    ) -> None:
        """Persist a completed work interval before the next external processing step."""
        row = connection.execute(
            """SELECT active_processing_started_at,status,retry_count
            FROM hl7_ingestion_attempts WHERE id=%s FOR UPDATE""",
            (attempt_id,),
        ).fetchone()
        if row is None or row[1] != "processing" or row[2] != retry_number:
            return
        started_at = row[0]
        ended_at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        elapsed = 0
        if started_at is not None:
            elapsed = connection.execute(
                """SELECT GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (%s - %s)) * 1000))::bigint""",
                (ended_at, started_at),
            ).fetchone()[0]
        connection.execute(
            """UPDATE hl7_ingestion_attempts SET
            processing_duration_ms=processing_duration_ms+%s,
            active_processing_started_at=%s,updated_at=%s WHERE id=%s""",
            (elapsed, ended_at, ended_at, attempt_id),
        )
        if retry_number > 0:
            connection.execute(
                """UPDATE hl7_ingestion_attempt_retries
                SET processing_duration_ms=processing_duration_ms+%s
                WHERE attempt_id=%s AND retry_number=%s AND completed_at IS NULL""",
                (elapsed, attempt_id, retry_number),
            )

    @staticmethod
    def _parsed_json(parsed: ParsedHl7Message) -> dict[str, object]:
        return {
            "metadata": asdict(parsed.metadata),
            "segments": parsed.segments,
            "normalized": parsed.normalized,
        }

    def _finish_error(
        self,
        attempt_id: UUID,
        status: str,
        code: str,
        message: str,
        retryable: bool,
        *,
        expected_retry_count: int = 0,
    ) -> dict[str, object]:
        try:
            with psycopg.connect(self.database_url) as connection:
                attempt = connection.execute(
                    "SELECT status,retry_count FROM hl7_ingestion_attempts WHERE id=%s FOR UPDATE",
                    (attempt_id,),
                ).fetchone()
                if (
                    attempt is None
                    or attempt[0] != "processing"
                    or attempt[1] != expected_retry_count
                ):
                    return self._attempt_response(connection, attempt_id)
                self._close_processing_generation(
                    connection,
                    attempt_id,
                    expected_retry_count,
                    result_status=status,
                    result_error_code=code,
                )
                connection.execute(
                    """UPDATE hl7_ingestion_attempts SET status=%s,outcome=%s,error_code=%s,
                    sanitized_error=%s,retryable=%s,
                    completed_at=CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                    updated_at=clock_timestamp(),active_processing_started_at=NULL,
                    lease_expires_at=CASE WHEN %s THEN NOW()+INTERVAL '30 seconds' ELSE NULL END,
                    raw_payload=CASE WHEN %s THEN raw_payload ELSE NULL END
                    WHERE id=%s AND status='processing' AND retry_count=%s""",
                    (
                        status,
                        status,
                        code,
                        message,
                        retryable,
                        retryable,
                        retryable,
                        retryable,
                        attempt_id,
                        expected_retry_count,
                    ),
                )
        except Exception as exc:
            logger.error(
                "Could not persist terminal HL7 ingestion audit state",
                extra={"error_type": type(exc).__name__},
            )
            return self._response(attempt_id, "audit_failed", "failed", "audit_update_failed", True)
        return self._response(attempt_id, status, status, code, retryable)

    @staticmethod
    def _response(
        attempt_id: UUID,
        status: str,
        outcome: str,
        error_code: str | None,
        retryable: bool,
        message_id: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "attempt_id": str(attempt_id),
            "status": status,
            "outcome": outcome,
            "error_code": error_code,
            "retryable": retryable,
        }
        if message_id:
            result["message_id"] = message_id
        return result

    def status(self, attempt_id: UUID) -> dict[str, object] | None:
        with psycopg.connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT status,outcome,error_code,sanitized_error,retryable,message_id,
                raw_object_key,raw_sha256,started_at,completed_at,retry_count,lease_expires_at,
                processing_duration_ms,active_processing_started_at
                FROM hl7_ingestion_attempts
                WHERE id=%s""",
                (attempt_id,),
            ).fetchone()
            retries = connection.execute(
                """SELECT retry_number,previous_status,previous_error_code,claimed_at,
                completed_at,result_status,processing_duration_ms,result_error_code
                FROM hl7_ingestion_attempt_retries
                WHERE attempt_id=%s ORDER BY retry_number""",
                (attempt_id,),
            ).fetchall()
        if row is None:
            return None
        return {
            "attempt_id": str(attempt_id),
            "status": row[0],
            "outcome": row[1],
            "error_code": row[2],
            "error": row[3],
            "retryable": row[4],
            "message_id": str(row[5]) if row[5] else None,
            "raw_object_key": row[6],
            "raw_sha256": row[7].strip(),
            "started_at": row[8].isoformat(),
            "completed_at": row[9].isoformat() if row[9] else None,
            "retry_count": row[10],
            "lease_expires_at": row[11].isoformat() if row[11] else None,
            "processing_duration_ms": row[12],
            "active_processing_started_at": row[13].isoformat() if row[13] else None,
            "retry_history": [
                {
                    "retry_number": retry[0],
                    "previous_status": retry[1],
                    "previous_error_code": retry[2],
                    "claimed_at": retry[3].isoformat(),
                    "completed_at": retry[4].isoformat() if retry[4] else None,
                    "result_status": retry[5],
                    "processing_duration_ms": retry[6],
                    "result_error_code": retry[7],
                }
                for retry in retries
            ],
        }

    def metrics(self, source_id: str | None = None) -> dict[str, int | float]:
        """Return aggregate operational counts without sender or patient identifiers."""
        with psycopg.connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT count(*) AS received,
                count(*) FILTER (WHERE status='accepted') AS accepted,
                count(*) FILTER (WHERE status='unchanged') AS duplicates,
                count(*) FILTER (WHERE status='conflict') AS conflicts,
                count(*) FILTER (WHERE status='rejected') AS rejected,
                count(*) FILTER (WHERE status='failed') AS failed,
                count(*) FILTER (WHERE status='processing') AS processing,
                COALESCE(
                    avg(processing_duration_ms) FILTER (WHERE completed_at IS NOT NULL) / 1000.0,
                    0
                ) AS avg_processing_duration_seconds,
                COALESCE(avg(EXTRACT(EPOCH FROM completed_at-started_at))
                    FILTER (WHERE completed_at IS NOT NULL),0)
                    AS avg_end_to_end_elapsed_seconds
                FROM hl7_ingestion_attempts
                WHERE (%s::text IS NULL OR source_id=%s)""",
                (source_id, source_id),
            ).fetchone()
        return {
            "received": row[0],
            "accepted": row[1],
            "duplicates": row[2],
            "conflicts": row[3],
            "rejected": row[4],
            "failed": row[5],
            "processing": row[6],
            "avg_processing_duration_seconds": float(row[7]),
            "avg_end_to_end_elapsed_seconds": float(row[8]),
        }

    def reconcile_stale_attempts(self, limit: int = 100) -> int:
        """Claim expired processing and retryable failed attempts and resume them safely."""
        if limit < 1 or limit > 1000:
            raise ValueError("Reconciliation limit must be between 1 and 1000")
        with psycopg.connect(self.database_url) as connection:
            candidates = connection.execute(
                """SELECT id,raw_payload,raw_sha256,raw_object_key,status,error_code,retry_count,
                lease_expires_at,updated_at FROM hl7_ingestion_attempts
                WHERE source_id=%s AND lease_expires_at <= clock_timestamp()
                AND (status='processing' OR (status='failed' AND retryable=TRUE))
                ORDER BY lease_expires_at,id LIMIT %s FOR UPDATE SKIP LOCKED""",
                (self.source_id, limit),
            ).fetchall()
            claimed: list[tuple[UUID, bytes, str, str, int]] = []
            for (
                attempt_id,
                payload,
                digest,
                object_key,
                previous_status,
                error_code,
                count,
                lease_expires_at,
                last_updated_at,
            ) in candidates:
                if count >= self.max_reconciliation_retries or payload is None:
                    terminal_code = (
                        "reconciliation_exhausted"
                        if count >= self.max_reconciliation_retries
                        else "recovery_payload_missing"
                    )
                    detail = (
                        "Stale processing attempt exceeded reconciliation retry limit"
                        if terminal_code == "reconciliation_exhausted"
                        else "Expired attempt has no retained raw payload"
                    )
                    checkpoint_at = min(last_updated_at, lease_expires_at)
                    self._close_processing_generation(
                        connection,
                        attempt_id,
                        count,
                        result_status="failed",
                        result_error_code=terminal_code,
                        ended_at=checkpoint_at,
                    )
                    connection.execute(
                        """UPDATE hl7_ingestion_attempts SET status='failed',outcome='failed',
                        error_code=%s,sanitized_error=%s,retryable=FALSE,
                        lease_expires_at=NULL,completed_at=clock_timestamp(),
                        active_processing_started_at=NULL,
                        updated_at=clock_timestamp()
                        WHERE id=%s AND status=%s AND retry_count=%s""",
                        (terminal_code, detail, attempt_id, previous_status, count),
                    )
                    continue
                next_count = count + 1
                if previous_status == "processing":
                    checkpoint_at = min(last_updated_at, lease_expires_at)
                    self._close_processing_generation(
                        connection,
                        attempt_id,
                        count,
                        result_status="failed",
                        result_error_code="lease_expired",
                        ended_at=checkpoint_at,
                    )
                updated = connection.execute(
                    """UPDATE hl7_ingestion_attempts SET status='processing',outcome='processing',
                    retryable=FALSE,completed_at=NULL,lease_expires_at=NOW()+INTERVAL '5 minutes',
                    retry_count=%s,updated_at=clock_timestamp(),
                    active_processing_started_at=clock_timestamp()
                    WHERE id=%s AND status=%s AND retry_count=%s
                    AND lease_expires_at <= clock_timestamp()
                    RETURNING id""",
                    (next_count, attempt_id, previous_status, count),
                ).fetchone()
                if updated is None:
                    continue
                connection.execute(
                    """INSERT INTO hl7_ingestion_attempt_retries
                    (attempt_id,retry_number,previous_status,previous_error_code)
                    VALUES (%s,%s,%s,%s)""",
                    (attempt_id, next_count, previous_status, error_code),
                )
                claimed.append((attempt_id, bytes(payload), digest.strip(), object_key, next_count))
        for attempt_id, payload, digest, object_key, retry_number in claimed:
            self._retry_attempt(attempt_id, payload, digest, object_key, retry_number)
        return len(claimed)

    def _retry_attempt(
        self,
        attempt_id: UUID,
        payload: bytes,
        digest: str,
        object_key: str,
        retry_number: int,
    ) -> None:
        """Resume the original durable attempt and reuse its content-addressed object."""
        try:
            self.object_store.ensure_bucket()
            self.object_store.put_raw_immutable(
                object_key, payload, digest, content_type="application/hl7-v2"
            )
            with psycopg.connect(self.database_url) as connection:
                self._checkpoint_processing_duration(connection, attempt_id, retry_number)
                updated = connection.execute(
                    """UPDATE hl7_ingestion_attempts SET silo_uploaded=TRUE,
                    updated_at=NOW(),lease_expires_at=NOW()+INTERVAL '5 minutes'
                    WHERE id=%s AND status='processing' AND retry_count=%s
                    RETURNING id""",
                    (attempt_id, retry_number),
                ).fetchone()
                if updated is None:
                    return
            parsed = parse_hl7_message(
                payload,
                supported_versions=self.supported_versions,
                encoding=self.encoding,
            )
            self._promote(
                attempt_id,
                digest,
                object_key,
                parsed,
                expected_retry_count=retry_number,
            )
        except Hl7MessageError as exc:
            terminal = "rejected" if exc.ack_code == "AR" else "failed"
            self._finish_error(
                attempt_id,
                terminal,
                exc.code,
                str(exc),
                False,
                expected_retry_count=retry_number,
            )
        except Exception as exc:
            logger.error(
                "HL7 reconciliation retry failed", extra={"error_type": type(exc).__name__}
            )
            self._finish_error(
                attempt_id,
                "failed",
                "dependency_failure",
                "HL7 message persistence failed; retry may be attempted",
                True,
                expected_retry_count=retry_number,
            )
