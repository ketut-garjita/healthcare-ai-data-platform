from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from healthcare_platform.storage.silo import SiloObjectStore


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "operation"
    )


def test_bucket_readiness_is_cached_after_success(monkeypatch) -> None:
    client = MagicMock()
    monkeypatch.setattr("healthcare_platform.storage.silo.boto3.client", lambda *_a, **_k: client)
    store = SiloObjectStore("http://silo:9000", "synthetic-key", "synthetic-secret", "raw")

    store.ensure_bucket()
    store.ensure_bucket()

    client.head_bucket.assert_called_once_with(Bucket="raw")
    client.create_bucket.assert_not_called()


def test_bucket_check_failure_is_not_cached_and_missing_bucket_is_recovered(monkeypatch) -> None:
    client = MagicMock()
    client.head_bucket.side_effect = [client_error("503", 503), None]
    monkeypatch.setattr("healthcare_platform.storage.silo.boto3.client", lambda *_a, **_k: client)
    store = SiloObjectStore("http://silo:9000", "synthetic-key", "synthetic-secret", "raw")

    with pytest.raises(ClientError):
        store.ensure_bucket()
    store.ensure_bucket()
    store.ensure_bucket()

    assert client.head_bucket.call_count == 2
    client.create_bucket.assert_not_called()


def test_missing_bucket_put_error_invalidates_readiness(monkeypatch) -> None:
    client = MagicMock()
    client.put_object.side_effect = client_error("NoSuchBucket", 404)
    monkeypatch.setattr("healthcare_platform.storage.silo.boto3.client", lambda *_a, **_k: client)
    store = SiloObjectStore("http://silo:9000", "synthetic-key", "synthetic-secret", "raw")
    store.ensure_bucket()

    with pytest.raises(ClientError):
        store.put_raw("synthetic/key", b"{}", "0" * 64)
    store.ensure_bucket()

    assert client.head_bucket.call_count == 2


def test_immutable_raw_write_reuses_only_matching_content_hash(monkeypatch) -> None:
    client = MagicMock()
    client.head_object.return_value = {"Metadata": {"sha256": "a" * 64}}
    monkeypatch.setattr("healthcare_platform.storage.silo.boto3.client", lambda *_a, **_k: client)
    store = SiloObjectStore("http://silo:9000", "synthetic-key", "synthetic-secret", "raw")

    store.put_raw_immutable("synthetic/key", b"synthetic", "a" * 64, "application/hl7-v2")

    client.put_object.assert_not_called()


def test_immutable_raw_write_rejects_content_address_collision(monkeypatch) -> None:
    client = MagicMock()
    client.head_object.return_value = {"Metadata": {"sha256": "b" * 64}}
    monkeypatch.setattr("healthcare_platform.storage.silo.boto3.client", lambda *_a, **_k: client)
    store = SiloObjectStore("http://silo:9000", "synthetic-key", "synthetic-secret", "raw")

    with pytest.raises(ValueError, match="different hash"):
        store.put_raw_immutable("synthetic/key", b"synthetic", "a" * 64, "application/hl7-v2")

    client.put_object.assert_not_called()
