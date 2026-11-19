"""Typed HL7 v2 adapter models and safe validation errors."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Hl7Metadata:
    sending_application: str | None = None
    sending_facility: str | None = None
    receiving_application: str | None = None
    receiving_facility: str | None = None
    message_timestamp: str | None = None
    message_code: str | None = None
    trigger_event: str | None = None
    message_structure: str | None = None
    message_control_id: str | None = None
    processing_id: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class ParsedHl7Message:
    metadata: Hl7Metadata
    segments: list[dict[str, Any]]
    normalized: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "segments": self.segments,
            "normalized": self.normalized,
        }


class Hl7MessageError(Exception):
    """Sanitized parsing/validation failure suitable for audit and ACK mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        ack_code: str = "AE",
        metadata: Hl7Metadata | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.ack_code = ack_code
        self.metadata = metadata or Hl7Metadata()
