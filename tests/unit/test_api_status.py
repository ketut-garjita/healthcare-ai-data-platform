import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import healthcare_platform.main as main_module
from healthcare_platform.config import settings
from healthcare_platform.integrations.fhir.client import FhirError
from healthcare_platform.main import app

client = TestClient(app)


def test_search_route_preserves_repeated_query_parameters(monkeypatch) -> None:
    observed = {}

    class FhirClient:
        def search(self, _resource_type, params):
            observed["params"] = params
            return {"resourceType": "Bundle", "type": "searchset", "entry": []}

        def close(self):
            pass

    monkeypatch.setattr(main_module, "_fhir_client", FhirClient)
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    response = client.get(
        "/integrations/fhir/Patient?identifier=synthetic-a&identifier=synthetic-b",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert observed["params"] == [
        ("identifier", "synthetic-a"),
        ("identifier", "synthetic-b"),
    ]
    assert response.json()["type"] == "searchset"


@pytest.mark.parametrize(
    ("upstream_status", "expected_status"),
    [(400, 422), (404, 404), (409, 409), (500, 502), (504, 504)],
)
def test_fhir_errors_map_to_safe_http_statuses(
    monkeypatch, upstream_status: int, expected_status: int
) -> None:
    class Client:
        def read(self, _resource_type, _resource_id):
            raise FhirError("safe upstream error", upstream_status=upstream_status)

        def close(self):
            pass

    monkeypatch.setattr(main_module, "_fhir_client", Client)
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    response = client.get(
        "/integrations/fhir/Patient/synthetic-not-found",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == expected_status
    assert "safe upstream error" in response.text


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ({"status": "ingested", "outcome": "created"}, 200),
        ({"status": "ingested", "outcome": "updated"}, 200),
        ({"status": "unchanged", "outcome": "unchanged"}, 200),
        ({"status": "ingested", "outcome": "historical_only"}, 200),
        ({"status": "ingested", "outcome": "conflict"}, 409),
        (
            {
                "status": "failed",
                "outcome": "failed",
                "error_category": "FhirError",
                "error": "Unsupported FHIR resource type",
            },
            422,
        ),
        (
            {
                "status": "failed",
                "outcome": "failed",
                "retryable": True,
                "error": "Safe storage failure",
            },
            503,
        ),
        ({"status": "audit_failed", "outcome": "failed", "error": "Safe audit failure"}, 503),
    ],
)
def test_ingestion_outcomes_map_to_http_statuses(monkeypatch, result, expected_status) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))

    class IngestionService:
        def ingest(self, _resource):
            return result

    monkeypatch.setattr(main_module, "_ingestion_service", IngestionService)
    response = client.post(
        "/ingestions/fhir",
        json={"resourceType": "Patient", "id": "synthetic-api-status"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == expected_status
    if expected_status >= 400:
        assert "test-token" not in response.text


@pytest.mark.parametrize(
    ("outcome", "lifecycle_status", "expected_http"),
    [
        ("created", "ingested", 200),
        ("updated", "ingested", 200),
        ("unchanged", "unchanged", 200),
        ("historical_only", "ingested", 200),
        ("conflict", "ingested", 409),
        ("failed", "failed", 503),
    ],
)
def test_post_response_matches_followup_status(
    monkeypatch, outcome, lifecycle_status, expected_http
) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    ingestion_id = "00000000-0000-0000-0000-000000000002"
    status_payload = {
        "ingestion_id": ingestion_id,
        "status": lifecycle_status,
        "outcome": outcome,
    }
    if outcome == "failed":
        status_payload.update({"error_category": "DependencyError", "retryable": True})

    class IngestionService:
        def ingest(self, _resource):
            return status_payload

        def status(self, _ingestion_id):
            return status_payload

    monkeypatch.setattr(main_module, "_ingestion_service", IngestionService)
    headers = {"Authorization": "Bearer test-token"}
    posted = client.post(
        "/ingestions/fhir",
        json={"resourceType": "Patient", "id": "synthetic-status"},
        headers=headers,
    )
    fetched = client.get(f"/ingestions/{ingestion_id}", headers=headers)
    assert posted.status_code == expected_http
    assert fetched.status_code == 200
    assert posted.json()["status"] == fetched.json()["status"]
    assert posted.json()["outcome"] == fetched.json()["outcome"]
