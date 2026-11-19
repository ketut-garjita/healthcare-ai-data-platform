"""Validated, auditable FHIR ingestion into Silo and PostgreSQL."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.types.json import Jsonb

from healthcare_platform.integrations.fhir.client import SUPPORTED_RESOURCE_TYPES, FhirError
from healthcare_platform.storage.silo import SiloObjectStore


def canonical_payload(resource: dict[str, Any]) -> bytes:
    return json.dumps(resource, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_hash(resource: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(resource)).hexdigest()


def validate_resource(resource: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(resource, dict):
        raise FhirError("FHIR resource must be a JSON object")
    resource_type, resource_id = resource.get("resourceType"), resource.get("id")
    if resource_type not in SUPPORTED_RESOURCE_TYPES:
        raise FhirError("Unsupported FHIR resource type")
    if not isinstance(resource_id, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", resource_id):
        raise FhirError("FHIR resource must contain a valid id")
    return resource_type, resource_id


def parse_last_updated(resource: dict[str, Any]) -> datetime | None:
    """Parse FHIR instant metadata; absent values are explicitly unordered."""
    meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
    value = meta.get("lastUpdated")
    if value is None:
        return None
    if not isinstance(value, str):
        raise FhirError("FHIR meta.lastUpdated must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FhirError("FHIR meta.lastUpdated must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FhirError("FHIR meta.lastUpdated must include a timezone")
    return parsed.astimezone(UTC)


class FhirIngestionService:
    def __init__(self, database_url: str, object_store: SiloObjectStore, source: str) -> None:
        self.database_url = database_url
        self.object_store = object_store
        self.source = self._safe_source(source)

    @staticmethod
    def _safe_source(source: str) -> str:
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return source
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))

    def ingest(self, resource: dict[str, Any]) -> dict[str, Any]:
        ingestion_id = uuid4()
        resource_type: str | None = None
        resource_id: str | None = None
        digest: str | None = None
        object_key: str | None = None
        raw: bytes | None = None

        try:
            resource_type, resource_id = validate_resource(resource)
            raw = canonical_payload(resource)
            digest = content_hash(resource)
            object_key = f"fhir/{resource_type}/{resource_id}/{digest}.json"
        except Exception as exc:
            return self._save_failure(
                ingestion_id, resource_type, resource_id, digest, object_key, resource, exc
            )

        # This transaction commits before any Silo operation. A process crash therefore
        # leaves a durable attempt and payload for later reconciliation.
        try:
            self._create_attempt(
                ingestion_id, resource_type, resource_id, digest, object_key, resource
            )
        except Exception:
            return {
                "ingestion_id": str(ingestion_id),
                "status": "audit_failed",
                "outcome": "failed",
                "error_category": "AuditPersistenceError",
                "error": "Could not persist ingestion attempt; no Silo write was performed",
            }

        try:
            self._upload_raw(ingestion_id, object_key, raw, digest)
            outcome = self._process_attempt(
                ingestion_id,
                resource_type,
                resource_id,
                digest,
                object_key,
                resource,
            )
        except Exception as exc:
            return self._save_failure(
                ingestion_id, resource_type, resource_id, digest, object_key, resource, exc
            )

        return {
            "ingestion_id": str(ingestion_id),
            "status": "unchanged" if outcome == "unchanged" else "ingested",
            "outcome": outcome,
            "content_hash": digest,
            "raw_object_key": object_key,
        }

    def _upload_raw(self, ingestion_id: UUID, object_key: str, raw: bytes, digest: str) -> None:
        """Upload immutable raw content before opening the resource promotion transaction."""
        self.object_store.ensure_bucket()
        self.object_store.put_raw(object_key, raw, digest)
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """UPDATE fhir_ingestion_attempts SET silo_uploaded=TRUE,
                lease_expires_at=NOW() + INTERVAL '5 minutes' WHERE ingestion_id=%s
                AND status='processing'""",
                (ingestion_id,),
            )

    def _create_attempt(
        self,
        ingestion_id: UUID,
        resource_type: str,
        resource_id: str,
        digest: str,
        object_key: str,
        resource: dict[str, Any],
    ) -> None:
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO fhir_ingestion_attempts
                (ingestion_id, source, resource_type, resource_id, content_hash,
                 raw_object_key, payload, status, lease_expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'processing', NOW() + INTERVAL '5 minutes')""",
                (
                    ingestion_id,
                    self.source,
                    resource_type,
                    resource_id,
                    digest,
                    object_key,
                    Jsonb(resource),
                ),
            )

    def _process_attempt(
        self,
        ingestion_id: UUID,
        resource_type: str,
        resource_id: str,
        digest: str,
        object_key: str,
        resource: dict[str, Any],
    ) -> str:
        meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
        version_id = meta.get("versionId")
        last_updated = parse_last_updated(resource)
        with psycopg.connect(self.database_url) as connection:
            attempt = connection.execute(
                """SELECT status FROM fhir_ingestion_attempts
                WHERE ingestion_id=%s FOR UPDATE""",
                (ingestion_id,),
            ).fetchone()
            if attempt is None or attempt[0] != "processing":
                raise RuntimeError("Ingestion attempt is not available for processing")
            connection.execute(
                """UPDATE fhir_ingestion_attempts SET
                lease_expires_at=NOW() + INTERVAL '5 minutes' WHERE ingestion_id=%s""",
                (ingestion_id,),
            )
            lock_key = json.dumps([self.source, resource_type, resource_id], separators=(",", ":"))
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            existing = connection.execute(
                """SELECT content_hash, last_updated FROM fhir_resources
                WHERE source=%s AND resource_type=%s AND resource_id=%s FOR UPDATE""",
                (self.source, resource_type, resource_id),
            ).fetchone()
            if existing and existing[0].strip() == digest:
                connection.execute(
                    """UPDATE fhir_ingestion_attempts SET status='unchanged', completed_at=NOW(),
                    outcome='unchanged', lease_expires_at=NULL WHERE ingestion_id=%s""",
                    (ingestion_id,),
                )
                return "unchanged"

            if existing is None:
                outcome = "created"
            else:
                current_updated = existing[1]
                if current_updated is None and last_updated is not None:
                    outcome = "updated"
                elif last_updated is None:
                    outcome = "historical_only"
                elif last_updated > current_updated:
                    outcome = "updated"
                elif last_updated < current_updated:
                    outcome = "historical_only"
                else:
                    outcome = "conflict"

            connection.execute(
                """INSERT INTO fhir_resource_versions
                (source, resource_type, resource_id, version_id, content_hash,
                 raw_object_key, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (
                    self.source,
                    resource_type,
                    resource_id,
                    version_id,
                    digest,
                    object_key,
                    Jsonb(resource),
                ),
            )
            if outcome == "created":
                connection.execute(
                    """INSERT INTO fhir_resources
                    (source, resource_type, resource_id, version_id, last_updated,
                     content_hash, raw_object_key, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        self.source,
                        resource_type,
                        resource_id,
                        version_id,
                        last_updated,
                        digest,
                        object_key,
                        Jsonb(resource),
                    ),
                )
            elif outcome == "updated":
                connection.execute(
                    """UPDATE fhir_resources SET version_id=%s, last_updated=%s,
                    content_hash=%s, raw_object_key=%s, payload=%s, last_ingested_at=NOW()
                    WHERE source=%s AND resource_type=%s AND resource_id=%s""",
                    (
                        version_id,
                        last_updated,
                        digest,
                        object_key,
                        Jsonb(resource),
                        self.source,
                        resource_type,
                        resource_id,
                    ),
                )
            connection.execute(
                """UPDATE fhir_ingestion_attempts SET status='ingested', outcome=%s,
                retryable=FALSE, silo_uploaded=TRUE, completed_at=NOW(), lease_expires_at=NULL
                WHERE ingestion_id=%s""",
                (outcome, ingestion_id),
            )
        return outcome

    def _save_failure(
        self,
        ingestion_id: UUID,
        resource_type: str | None,
        resource_id: str | None,
        digest: str | None,
        object_key: str | None,
        resource: Any,
        exc: Exception,
    ) -> dict[str, Any]:
        safe_message = self._safe_error(exc)
        retryable = isinstance(exc, (BotoCoreError, ClientError, psycopg.Error))
        try:
            with psycopg.connect(self.database_url) as connection:
                cursor = connection.execute(
                    """INSERT INTO fhir_ingestion_attempts
                    (ingestion_id, source, resource_type, resource_id, content_hash,
                     raw_object_key, payload, status, outcome, retryable, completed_at,
                     lease_expires_at,
                     error_category, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed', 'failed', %s, NOW(),
                    CASE WHEN %s THEN NOW() + INTERVAL '30 seconds' ELSE NULL END, %s, %s)
                    ON CONFLICT (ingestion_id) DO UPDATE SET status='failed',
                    outcome='failed', retryable=EXCLUDED.retryable, completed_at=NOW(),
                    lease_expires_at=EXCLUDED.lease_expires_at,
                    error_category=EXCLUDED.error_category,
                    error_message=EXCLUDED.error_message
                    WHERE fhir_ingestion_attempts.status='processing'""",
                    (
                        ingestion_id,
                        self.source,
                        resource_type or "unknown",
                        resource_id,
                        digest,
                        object_key,
                        Jsonb(resource) if isinstance(resource, dict) else None,
                        retryable,
                        retryable,
                        type(exc).__name__,
                        safe_message,
                    ),
                )
                if cursor.rowcount == 0:
                    existing = connection.execute(
                        """SELECT status, outcome, retryable FROM fhir_ingestion_attempts
                        WHERE ingestion_id=%s""",
                        (ingestion_id,),
                    ).fetchone()
                    if existing is not None:
                        return {
                            "ingestion_id": str(ingestion_id),
                            "status": existing[0],
                            "outcome": existing[1],
                            "retryable": existing[2],
                        }
        except Exception:
            return {
                "ingestion_id": str(ingestion_id),
                "status": "audit_failed",
                "outcome": "failed",
                "error_category": "AuditPersistenceError",
                "error": "Ingestion failed and its audit status could not be persisted",
            }
        return {
            "ingestion_id": str(ingestion_id),
            "status": "failed",
            "outcome": "failed",
            "retryable": retryable,
            "error_category": type(exc).__name__,
            "error": safe_message,
        }

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, FhirError):
            return str(exc)[:500]
        if isinstance(exc, (BotoCoreError, ClientError)):
            return "Raw object storage operation failed"
        if isinstance(exc, psycopg.Error):
            return "PostgreSQL persistence failed"
        return "FHIR ingestion failed"

    def reconcile_stale_attempts(
        self, limit: int = 50, max_retries: int = 5
    ) -> list[dict[str, str]]:
        """Claim and retry expired attempts; Silo objects are never deleted."""
        if limit < 1 or max_retries < 1:
            raise ValueError("limit and max_retries must be positive")
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """UPDATE fhir_ingestion_attempts
                SET status='failed', outcome='failed', retryable=FALSE, completed_at=NOW(),
                    lease_expires_at=NULL, error_category='RetryLimit',
                    error_message='Ingestion retry limit reached'
                WHERE (status='processing' OR retryable=TRUE) AND retry_count >= %s
                  AND COALESCE(lease_expires_at, started_at + INTERVAL '5 minutes') < NOW()""",
                (max_retries,),
            )
            rows = connection.execute(
                """WITH candidates AS (
                    SELECT ingestion_id FROM fhir_ingestion_attempts
                    WHERE (status='processing' OR retryable=TRUE) AND retry_count < %s
                      AND COALESCE(lease_expires_at, started_at + INTERVAL '5 minutes') < NOW()
                    ORDER BY started_at
                    FOR UPDATE SKIP LOCKED LIMIT %s
                )
                UPDATE fhir_ingestion_attempts AS attempts
                SET status='processing', retryable=FALSE,
                    retry_count=attempts.retry_count + 1,
                    lease_expires_at=NOW() + INTERVAL '5 minutes', completed_at=NULL
                FROM candidates
                WHERE attempts.ingestion_id=candidates.ingestion_id
                RETURNING attempts.ingestion_id, attempts.resource_type,
                          attempts.resource_id, attempts.content_hash,
                          attempts.raw_object_key, attempts.payload""",
                (max_retries, limit),
            ).fetchall()

        results: list[dict[str, str]] = []
        for row in rows:
            ingestion_id, resource_type, resource_id, digest, object_key, resource = row
            if not isinstance(resource, dict):
                result = self._save_failure(
                    ingestion_id,
                    resource_type,
                    resource_id,
                    digest,
                    object_key,
                    resource,
                    FhirError("Retry payload is unavailable"),
                )
                results.append({"ingestion_id": str(ingestion_id), "status": result["status"]})
                continue
            try:
                raw = canonical_payload(resource)
                self._upload_raw(ingestion_id, object_key, raw, digest)
                outcome = self._process_attempt(
                    ingestion_id,
                    resource_type,
                    resource_id,
                    digest,
                    object_key,
                    resource,
                )
                results.append(
                    {
                        "ingestion_id": str(ingestion_id),
                        "status": "unchanged" if outcome == "unchanged" else "ingested",
                        "outcome": outcome,
                    }
                )
            except Exception as exc:
                result = self._save_failure(
                    ingestion_id, resource_type, resource_id, digest, object_key, resource, exc
                )
                results.append({"ingestion_id": str(ingestion_id), "status": result["status"]})
        return results

    def status(self, ingestion_id: UUID) -> dict[str, Any] | None:
        with psycopg.connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT ingestion_id, source, resource_type, resource_id, content_hash,
                status, outcome, started_at, completed_at, raw_object_key,
                error_category, error_message, retry_count, lease_expires_at, silo_uploaded
                , retryable
                FROM fhir_ingestion_attempts WHERE ingestion_id=%s""",
                (ingestion_id,),
            ).fetchone()
        if row is None:
            return None
        names = (
            "ingestion_id",
            "source",
            "resource_type",
            "resource_id",
            "content_hash",
            "status",
            "outcome",
            "started_at",
            "completed_at",
            "raw_object_key",
            "error_category",
            "error_message",
            "retry_count",
            "lease_expires_at",
            "silo_uploaded",
            "retryable",
        )
        result = dict(zip(names, row, strict=True))
        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, UUID):
                result[key] = str(value)
        return result
