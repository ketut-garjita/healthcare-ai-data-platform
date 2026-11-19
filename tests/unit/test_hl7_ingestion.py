from healthcare_platform.ingestion.hl7 import Hl7IngestionService, hl7_content_hash

MESSAGE = b"MSH|^~\\&|APP|FAC|REC|RFAC|20250101120000||ADT^A01^ADT_A01|ctl-1|P|2.5.1\r"


class NoWriteStore:
    def ensure_bucket(self) -> None:
        raise AssertionError("Silo must not be touched before attempt persistence")

    def put_raw(self, *args, **kwargs) -> None:
        raise AssertionError("Silo must not be touched before attempt persistence")


def test_hl7_content_hash_uses_exact_message_bytes() -> None:
    assert hl7_content_hash(MESSAGE) != hl7_content_hash(MESSAGE + b"\r")


def test_durable_attempt_failure_stops_before_silo(monkeypatch) -> None:
    service = Hl7IngestionService("postgresql://unused", NoWriteStore())

    def fail_attempt(*args, **kwargs) -> None:
        raise RuntimeError("synthetic database outage")

    monkeypatch.setattr(service, "_create_attempt", fail_attempt)
    result = service.ingest(MESSAGE)
    assert result["status"] == "failed"
    assert result["outcome"] == "audit_failed"
    assert result["error_code"] == "audit_persistence_failed"
