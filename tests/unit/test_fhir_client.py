import httpx
import pytest

from healthcare_platform.integrations.fhir.client import (
    FhirCapabilityCache,
    FhirClient,
    FhirError,
)

CAPABILITY = {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"}


def client_for(handler) -> FhirClient:
    return FhirClient("http://fhir.test/fhir", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("content_length", [None, "1", "99999"])
def test_response_size_limit_streams_actual_bytes_and_closes_response(content_length) -> None:
    headers = {} if content_length is None else {"Content-Length": content_length}
    body = b'{"resourceType":"CapabilityStatement","fhirVersion":"4.0.1"}'
    response = httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
    client = FhirClient(
        "http://fhir.test/fhir",
        transport=httpx.MockTransport(lambda _request: response),
        max_response_bytes=20,
    )

    with pytest.raises(FhirError, match="exceeded the configured size limit"):
        client.capability_statement()
    assert response.is_closed
    client.close()


def test_response_size_limit_allows_normal_response() -> None:
    response = httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'))
    client = FhirClient(
        "http://fhir.test/fhir",
        transport=httpx.MockTransport(lambda _request: response),
        max_response_bytes=20,
    )
    assert client._request("GET", "metadata") == {"ok": True}
    assert response.is_closed
    client.close()


def test_capability_statement_checks_r4() -> None:
    client = client_for(lambda request: httpx.Response(200, json=CAPABILITY))
    assert client.verify_r4()["fhirVersion"] == "4.0.1"
    client.close()


def test_capability_cache_is_shared_bounded_by_ttl_and_only_caches_success(monkeypatch) -> None:
    requests: list[str] = []
    now = 100.0

    def fake_monotonic():
        return now

    monkeypatch.setattr(
        "healthcare_platform.integrations.fhir.client.time.monotonic", fake_monotonic
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=CAPABILITY)

    cache = FhirCapabilityCache(ttl_seconds=5)
    first = FhirClient(
        "http://fhir.test/fhir", transport=httpx.MockTransport(handler), capability_cache=cache
    )
    second = FhirClient(
        "http://fhir.test/fhir", transport=httpx.MockTransport(handler), capability_cache=cache
    )
    first.verify_r4()
    second.verify_r4()
    assert len(requests) == 1
    now += 6
    second.verify_r4()
    assert len(requests) == 2
    first.close()
    second.close()

    attempts = 0

    def recovering_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts == 1 else httpx.Response(200, json=CAPABILITY)

    recovery_client = FhirClient(
        "http://recover.test/fhir",
        transport=httpx.MockTransport(recovering_handler),
        capability_cache=FhirCapabilityCache(ttl_seconds=60),
    )
    with pytest.raises(FhirError):
        recovery_client.verify_r4()
    assert recovery_client.verify_r4()["fhirVersion"] == "4.0.1"
    assert attempts == 2
    recovery_client.close()


@pytest.mark.parametrize("max_pages", [0, 101])
def test_search_page_limit_is_always_bounded(max_pages: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        FhirClient("http://fhir.test/fhir", max_pages=max_pages)


def test_capability_rejects_wrong_fhir_version() -> None:
    client = client_for(
        lambda request: httpx.Response(200, json={**CAPABILITY, "fhirVersion": "5.0.0"})
    )
    with pytest.raises(FhirError, match="does not advertise"):
        client.verify_r4()
    client.close()


def test_search_parses_bundle_entries() -> None:
    patient = {"resourceType": "Patient", "id": "synthetic-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(
            200, json={"resourceType": "Bundle", "entry": [{"resource": patient}]}
        )

    client = client_for(handler)
    bundle = client.search("Patient")
    assert bundle["entry"] == [{"resource": patient}]
    client.close()


def test_search_preserves_bundle_metadata_and_follows_pages() -> None:
    patient_a = {"resourceType": "Patient", "id": "synthetic-a"}
    patient_b = {"resourceType": "Patient", "id": "synthetic-b"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "entry": [{"resource": patient_b}],
                    "link": [{"relation": "self", "url": str(request.url)}],
                },
            )
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "id": "synthetic-search-bundle",
                "type": "searchset",
                "total": 2,
                "entry": [{"resource": patient_a}],
                "link": [
                    {"relation": "self", "url": str(request.url)},
                    {"relation": "next", "url": "http://fhir.test/fhir/Patient?page=2"},
                ],
            },
        )

    client = client_for(handler)
    bundle = client.search("Patient")
    assert bundle["id"] == "synthetic-search-bundle"
    assert bundle["type"] == "searchset"
    assert bundle["total"] == 2
    assert [item["resource"]["id"] for item in bundle["entry"]] == [
        "synthetic-a",
        "synthetic-b",
    ]
    assert not any(link["relation"] == "next" for link in bundle["link"])
    client.close()


def test_search_preserves_repeated_query_parameters() -> None:
    observed: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        observed.append(request.url.params.multi_items())
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    client = client_for(handler)
    client.search("Patient", [("identifier", "synthetic-a"), ("identifier", "synthetic-b")])
    assert observed == [[("identifier", "synthetic-a"), ("identifier", "synthetic-b")]]
    client.close()


def test_search_rejects_malformed_entries_and_foreign_next_links() -> None:
    def malformed_entry(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [None]})

    client = client_for(malformed_entry)
    with pytest.raises(FhirError, match="malformed entry"):
        client.search("Patient")
    client.close()

    requests: list[str] = []

    def foreign_next(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": "https://attacker.invalid/steal"}],
            },
        )

    client = client_for(foreign_next)
    with pytest.raises(FhirError, match="outside the configured server"):
        client.search("Patient")
    assert not any("attacker.invalid" in url for url in requests)
    client.close()


def test_search_respects_maximum_page_count_and_returns_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": "Patient?page=2"}],
            },
        )

    client = FhirClient(
        "http://fhir.test/fhir", transport=httpx.MockTransport(handler), max_pages=1
    )
    bundle = client.search("Patient")
    assert bundle["link"] == [{"relation": "next", "url": "Patient?page=2"}]
    client.close()


def test_search_rejects_malformed_next_link_and_times_out_safely() -> None:
    def malformed_link(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": "http://[invalid"}],
            },
        )

    client = client_for(malformed_link)
    with pytest.raises(FhirError, match="malformed"):
        client.search("Patient")
    client.close()

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    client = client_for(timeout)
    with pytest.raises(FhirError, match="timed out") as error:
        client.capability_statement()
    assert error.value.upstream_status == 504
    client.close()


def test_create_read_update_use_expected_methods() -> None:
    seen: list[tuple[str, str]] = []
    resource = {"resourceType": "Patient", "id": "synthetic-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("metadata"):
            return httpx.Response(200, json=CAPABILITY)
        return httpx.Response(200, json=resource)

    client = client_for(handler)
    client.create(resource)
    client.read("Patient", "synthetic-1")
    client.update(resource)
    assert [method for method, _ in seen if method != "GET" or not _.endswith("metadata")] == [
        "POST",
        "GET",
        "PUT",
    ]
    client.close()


@pytest.mark.parametrize("body", ["not-json", "[]"])
def test_malformed_capability_is_rejected(body: str) -> None:
    client = client_for(lambda request: httpx.Response(200, content=body))
    with pytest.raises(FhirError):
        client.capability_statement()
    client.close()


def test_http_error_is_safe() -> None:
    client = client_for(lambda request: httpx.Response(500, json={"detail": "private payload"}))
    with pytest.raises(FhirError, match="HTTP 500") as error:
        client.capability_statement()
    assert "private payload" not in str(error.value)
    client.close()
