import secrets
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from healthcare_platform.config import settings
from healthcare_platform.ingestion.fhir import FhirIngestionService
from healthcare_platform.ingestion.hl7 import Hl7IngestionService
from healthcare_platform.integrations.fhir.client import FhirCapabilityCache, FhirClient, FhirError
from healthcare_platform.storage.silo import SiloObjectStore

app = FastAPI(
    title="Healthcare AI Data Platform API",
    version="0.1.0",
    description="Development API for the Healthcare AI Data Platform portfolio project.",
)
bearer_scheme = HTTPBearer(auto_error=False)
_capability_cache = FhirCapabilityCache(settings.fhir_capability_cache_seconds)


def require_fhir_api_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    configured = settings.fhir_api_bearer_token
    if configured is None:
        raise HTTPException(status_code=503, detail="FHIR API authentication is not configured")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode("utf-8"), configured.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _fhir_client() -> FhirClient:
    return FhirClient(
        settings.fhir_base_url,
        settings.fhir_timeout_seconds,
        max_pages=settings.fhir_search_max_pages,
        max_response_bytes=settings.fhir_max_response_bytes,
        capability_cache=_capability_cache,
    )


@lru_cache(maxsize=1)
def _object_store() -> SiloObjectStore:
    store = SiloObjectStore(
        settings.silo_endpoint,
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    return store


def _ingestion_service() -> FhirIngestionService:
    return FhirIngestionService(settings.database_url, _object_store(), settings.fhir_base_url)


def _hl7_ingestion_service() -> Hl7IngestionService:
    return Hl7IngestionService(
        settings.database_url,
        _object_store(),
        settings.hl7_source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )


def _raise_fhir_error(exc: FhirError) -> None:
    upstream_status = exc.upstream_status
    if exc.client_error or upstream_status in {400, 422}:
        status_code = 422
    elif upstream_status == 404:
        status_code = 404
    elif upstream_status in {409, 412}:
        status_code = 409
    elif upstream_status == 504:
        status_code = 504
    else:
        status_code = 502
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get(
    "/integrations/fhir/capability", tags=["FHIR"], dependencies=[Depends(require_fhir_api_token)]
)
def fhir_capability() -> dict[str, Any]:
    client = _fhir_client()
    try:
        return client.verify_r4()
    except FhirError as exc:
        _raise_fhir_error(exc)
    finally:
        client.close()


@app.get(
    "/integrations/fhir/{resource_type}",
    tags=["FHIR"],
    dependencies=[Depends(require_fhir_api_token)],
)
def search_fhir(resource_type: str, request: Request) -> dict[str, Any]:
    params = list(request.query_params.multi_items())
    client = _fhir_client()
    try:
        return client.search(resource_type, params)
    except FhirError as exc:
        _raise_fhir_error(exc)
    finally:
        client.close()


@app.get(
    "/integrations/fhir/{resource_type}/{resource_id}",
    tags=["FHIR"],
    dependencies=[Depends(require_fhir_api_token)],
)
def read_fhir(resource_type: str, resource_id: str) -> dict[str, Any]:
    client = _fhir_client()
    try:
        return client.read(resource_type, resource_id)
    except FhirError as exc:
        _raise_fhir_error(exc)
    finally:
        client.close()


@app.post(
    "/integrations/fhir/resources",
    tags=["FHIR"],
    dependencies=[Depends(require_fhir_api_token)],
)
def create_fhir(resource: dict[str, Any]) -> dict[str, Any]:
    client = _fhir_client()
    try:
        return client.create(resource)
    except FhirError as exc:
        _raise_fhir_error(exc)
    finally:
        client.close()


@app.put(
    "/integrations/fhir/resources",
    tags=["FHIR"],
    dependencies=[Depends(require_fhir_api_token)],
)
def update_fhir(resource: dict[str, Any]) -> dict[str, Any]:
    client = _fhir_client()
    try:
        return client.update(resource)
    except FhirError as exc:
        _raise_fhir_error(exc)
    finally:
        client.close()


@app.post(
    "/ingestions/fhir",
    tags=["FHIR Ingestion"],
    dependencies=[Depends(require_fhir_api_token)],
)
def ingest_fhir(resource: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _ingestion_service().ingest(resource)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="FHIR ingestion services are unavailable"
        ) from exc
    outcome = result.get("outcome", result.get("status"))
    response_status = 200
    if outcome == "conflict":
        response_status = 409
    elif result.get("status") == "failed" and not result.get("retryable"):
        response_status = 422 if result.get("error_category") == "FhirError" else 503
    elif result.get("status") in {"retryable", "audit_failed"} or result.get("retryable"):
        response_status = 503
    if response_status != 200:
        return JSONResponse(status_code=response_status, content=result)
    return result


@app.get(
    "/ingestions/{ingestion_id}",
    tags=["FHIR Ingestion"],
    dependencies=[Depends(require_fhir_api_token)],
)
def get_ingestion_status(ingestion_id: UUID) -> dict[str, Any]:
    try:
        status = _ingestion_service().status(ingestion_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FHIR ingestion status is unavailable") from exc
    if status is None:
        raise HTTPException(status_code=404, detail="Ingestion attempt not found")
    return status


@app.get(
    "/ingestions/hl7/{attempt_id}",
    tags=["HL7 v2 Ingestion"],
    dependencies=[Depends(require_fhir_api_token)],
)
def get_hl7_ingestion_status(attempt_id: UUID) -> dict[str, Any]:
    try:
        status = _hl7_ingestion_service().status(attempt_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="HL7 ingestion status is unavailable") from exc
    if status is None:
        raise HTTPException(status_code=404, detail="HL7 ingestion attempt not found")
    return status


@app.get(
    "/integrations/hl7/metrics",
    tags=["HL7 v2 Ingestion"],
    dependencies=[Depends(require_fhir_api_token)],
)
def hl7_metrics() -> dict[str, int | float]:
    try:
        return _hl7_ingestion_service().metrics()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="HL7 metrics are unavailable") from exc


@app.get("/health", tags=["Operations"])
def health() -> dict[str, str]:
    # Liveness probe: confirms that the API process is running.
    return {"status": "ok", "service": "healthcare-api"}


@app.get("/ready", tags=["Operations"])
def readiness() -> dict[str, str]:
    # Readiness probe: checks connectivity to PostgreSQL.
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc

    return {"status": "ready", "database": "connected"}
