import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import healthcare_platform.main as main_module
from healthcare_platform.config import Settings, settings
from healthcare_platform.main import app

client = TestClient(app)


def test_empty_token_configuration_is_unconfigured() -> None:
    configured = Settings(_env_file=None, fhir_api_bearer_token="")
    assert configured.fhir_api_bearer_token is None


def fake_fhir_client():
    class Client:
        def verify_r4(self):
            return {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"}

        def search(self, resource_type, _params):
            return {"resourceType": "Bundle", "type": "searchset", "entry": []}

        def read(self, resource_type, resource_id):
            return {"resourceType": resource_type, "id": resource_id}

        def create(self, resource):
            return {**resource, "id": "synthetic-auth-test"}

        def update(self, resource):
            return resource

        def close(self):
            pass

    return Client()


READ_ROUTES = [
    ("GET", "/integrations/fhir/capability"),
    ("GET", "/integrations/fhir/Patient?identifier=synthetic-test"),
    ("GET", "/integrations/fhir/Patient/synthetic-test"),
    ("GET", "/ingestions/00000000-0000-0000-0000-000000000001"),
]


def request_route(method, path, headers=None):
    return client.request(method, path, headers=headers)


@pytest.mark.parametrize(("method", "path"), READ_ROUTES)
def test_all_fhir_read_routes_reject_missing_and_invalid_tokens(monkeypatch, method, path) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    assert request_route(method, path).status_code == 401
    invalid = request_route(method, path, {"Authorization": "Bearer invalid-token"})
    assert invalid.status_code == 401
    assert "invalid-token" not in invalid.text


@pytest.mark.parametrize(("method", "path"), READ_ROUTES)
def test_all_fhir_read_routes_accept_valid_token(monkeypatch, method, path) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    monkeypatch.setattr(main_module, "_fhir_client", fake_fhir_client)

    class IngestionService:
        def status(self, _ingestion_id):
            return {"status": "ingested", "outcome": "created"}

    monkeypatch.setattr(main_module, "_ingestion_service", IngestionService)
    response = request_route(method, path, {"Authorization": "Bearer test-token"})
    assert response.status_code == 200


def test_mutating_routes_fail_closed_when_token_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", None)
    response = client.post("/integrations/fhir/resources", json={"resourceType": "Patient"})
    assert response.status_code == 503
    assert "token" not in response.text


def test_mutating_routes_reject_missing_and_invalid_tokens(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    payload = {"resourceType": "Patient", "id": "synthetic-auth-test"}
    assert client.post("/integrations/fhir/resources", json=payload).status_code == 401
    response = client.put(
        "/integrations/fhir/resources",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert "wrong-token" not in response.text


def test_valid_token_allows_mutating_routes_and_health_stays_public(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fhir_api_bearer_token", SecretStr("test-token"))
    monkeypatch.setattr(main_module, "_fhir_client", fake_fhir_client)

    class IngestionService:
        def ingest(self, resource):
            return {"status": "ingested", "resourceType": resource["resourceType"]}

    monkeypatch.setattr(main_module, "_ingestion_service", IngestionService)

    headers = {"Authorization": "Bearer test-token"}
    created = client.post(
        "/integrations/fhir/resources",
        json={"resourceType": "Patient"},
        headers=headers,
    )
    updated = client.put(
        "/integrations/fhir/resources",
        json={"resourceType": "Patient", "id": "synthetic-auth-test"},
        headers=headers,
    )
    ingested = client.post(
        "/ingestions/fhir",
        json={"resourceType": "Patient", "id": "synthetic-auth-test"},
        headers=headers,
    )

    assert created.status_code == 200
    assert updated.status_code == 200
    assert ingested.status_code == 200
    assert client.get("/health").status_code == 200
