"""Small synchronous client for the FHIR REST API."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

SUPPORTED_RESOURCE_TYPES = frozenset({"Patient", "Practitioner", "Encounter", "Observation"})
FHIR_R4_VERSION = "4.0.1"


class FhirError(Exception):
    """Safe, user-facing FHIR communication or response error."""

    def __init__(
        self,
        message: str,
        upstream_status: int | None = None,
        *,
        client_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status
        self.client_error = client_error


class FhirCapabilityCache:
    """Small TTL cache of successfully verified CapabilityStatements."""

    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 32) -> None:
        if ttl_seconds < 0 or max_entries < 1:
            raise ValueError("cache TTL must be non-negative and max_entries positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_load(self, key: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self.ttl_seconds == 0:
            return loader()
        with self._lock:
            now = time.monotonic()
            cached = self._entries.get(key)
            if cached is not None and cached[0] > now:
                self._entries.move_to_end(key)
                return deepcopy(cached[1])
            self._entries.pop(key, None)
            value = loader()
            self._entries[key] = (time.monotonic() + self.ttl_seconds, deepcopy(value))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return deepcopy(value)


class FhirClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        max_pages: int = 10,
        max_response_bytes: int = 10 * 1024 * 1024,
        capability_cache: FhirCapabilityCache | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.max_pages = max_pages
        self.max_response_bytes = max_response_bytes
        self._capability_cache = capability_cache or FhirCapabilityCache(ttl_seconds=0)
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout_seconds,
            headers={"Accept": "application/fhir+json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def capability_statement(self) -> dict[str, Any]:
        data = self._request("GET", "metadata")
        if data.get("resourceType") != "CapabilityStatement":
            raise FhirError("FHIR server returned an invalid CapabilityStatement")
        return data

    def verify_r4(self) -> dict[str, Any]:
        def load_and_verify() -> dict[str, Any]:
            statement = self.capability_statement()
            if statement.get("fhirVersion") != FHIR_R4_VERSION:
                raise FhirError("FHIR server does not advertise FHIR R4 (4.0.1)")
            return statement

        return self._capability_cache.get_or_load(self.base_url, load_and_verify)

    def search(
        self,
        resource_type: str,
        params: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        self._validate_type(resource_type)
        self.verify_r4()
        query: Any = params or {}
        first = self._request("GET", quote(resource_type), params=query)
        self._validate_bundle(first)
        bundle = dict(first)
        entries = list(first.get("entry", []))
        pages = 1
        page = first
        while pages < self.max_pages:
            next_url = self._next_url(page)
            if next_url is None:
                break
            page = self._request("GET", next_url)
            self._validate_bundle(page)
            entries.extend(page.get("entry", []))
            pages += 1
        bundle["entry"] = entries
        first_links = first.get("link", [])
        if isinstance(first_links, list):
            links = [link for link in first_links if link.get("relation") != "next"]
            remaining_next = self._next_link(page)
            if remaining_next is not None:
                links.append(remaining_next)
            bundle["link"] = links
        return bundle

    def _validate_bundle(self, bundle: dict[str, Any]) -> None:
        entries = bundle.get("entry", [])
        if bundle.get("resourceType") != "Bundle" or not isinstance(entries, list):
            raise FhirError("FHIR search returned an invalid Bundle")
        for entry in entries:
            if not isinstance(entry, dict):
                raise FhirError("FHIR search Bundle contains a malformed entry")
            resource = entry.get("resource")
            if not isinstance(resource, dict) or not isinstance(resource.get("resourceType"), str):
                raise FhirError("FHIR search Bundle entry has no valid resource")
        self._next_link(bundle)

    @staticmethod
    def _next_link(bundle: dict[str, Any]) -> dict[str, Any] | None:
        links = bundle.get("link", [])
        if not isinstance(links, list):
            raise FhirError("FHIR search Bundle contains malformed links")
        for link in links:
            if not isinstance(link, dict) or not isinstance(link.get("relation"), str):
                raise FhirError("FHIR search Bundle contains a malformed link")
            if link["relation"] == "next":
                if not isinstance(link.get("url"), str):
                    raise FhirError("FHIR search Bundle next link has no URL")
                return link
        return None

    def _next_url(self, bundle: dict[str, Any]) -> str | None:
        link = self._next_link(bundle)
        if link is None:
            return None
        try:
            target = urljoin(f"{self.base_url}/", link["url"])
            expected = urlsplit(self.base_url)
            actual = urlsplit(target)
            expected_origin = (
                expected.scheme.lower(),
                expected.hostname.lower() if expected.hostname else None,
                expected.port or (443 if expected.scheme.lower() == "https" else 80),
            )
            actual_origin = (
                actual.scheme.lower(),
                actual.hostname.lower() if actual.hostname else None,
                actual.port or (443 if actual.scheme.lower() == "https" else 80),
            )
        except ValueError as exc:
            raise FhirError("FHIR search Bundle next link is malformed") from exc
        base_path = expected.path.rstrip("/")
        if (
            actual_origin != expected_origin
            or actual.username is not None
            or actual.password is not None
            or not (actual.path == base_path or actual.path.startswith(f"{base_path}/"))
            or actual.fragment
        ):
            raise FhirError("FHIR search Bundle next link is outside the configured server")
        return target

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        self._validate_type(resource_type)
        self.verify_r4()
        return self._resource_response(
            self._request("GET", f"{quote(resource_type)}/{quote(resource_id, safe='')}"),
            resource_type,
        )

    def create(self, resource: dict[str, Any]) -> dict[str, Any]:
        resource_type = self._validate_resource(resource)
        self.verify_r4()
        return self._resource_response(
            self._request("POST", quote(resource_type), json=resource), resource_type
        )

    def update(self, resource: dict[str, Any]) -> dict[str, Any]:
        resource_type = self._validate_resource(resource)
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise FhirError("FHIR update requires a resource id", client_error=True)
        self.verify_r4()
        return self._resource_response(
            self._request(
                "PUT", f"{quote(resource_type)}/{quote(resource_id, safe='')}", json=resource
            ),
            resource_type,
        )

    @staticmethod
    def _validate_type(resource_type: str) -> str:
        if resource_type not in SUPPORTED_RESOURCE_TYPES:
            raise FhirError("Unsupported FHIR resource type", client_error=True)
        return resource_type

    @classmethod
    def _validate_resource(cls, resource: dict[str, Any]) -> str:
        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str):
            raise FhirError("FHIR resource must contain a resourceType", client_error=True)
        return cls._validate_type(resource_type)

    @staticmethod
    def _resource_response(data: dict[str, Any], resource_type: str) -> dict[str, Any]:
        if data.get("resourceType") != resource_type or not isinstance(data.get("id"), str):
            raise FhirError("FHIR server returned an invalid resource")
        return data

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with self._client.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise FhirError("FHIR server response exceeded the configured size limit")
                    body.extend(chunk)
            result = httpx.Response(200, content=bytes(body)).json()
        except FhirError:
            raise
        except httpx.TimeoutException as exc:
            raise FhirError("FHIR server request timed out", upstream_status=504) from exc
        except httpx.HTTPStatusError as exc:
            raise FhirError(
                f"FHIR server returned HTTP {exc.response.status_code}",
                upstream_status=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirError("FHIR server could not be reached") from exc
        except ValueError as exc:
            raise FhirError("FHIR server returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise FhirError("FHIR server returned an invalid JSON object")
        return result
