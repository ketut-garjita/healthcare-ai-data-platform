"""HL7 v2 application acknowledgments, independent of the MLLP transport."""

from datetime import UTC, datetime
from uuid import uuid4

from healthcare_platform.integrations.hl7.models import Hl7Metadata
from healthcare_platform.integrations.hl7.parser import extract_msh_metadata

MLLP_START = b"\x0b"
MLLP_END = b"\x1c"
MLLP_CR = b"\x0d"
ACK_CODES = frozenset({"AA", "AE", "AR"})


def _safe_field(value: str | None) -> str:
    if not value:
        return ""
    return "".join(" " if char in "|^~\\&\r\n" or ord(char) < 32 else char for char in value)


def build_ack(
    raw_message: bytes,
    code: str,
    *,
    metadata: Hl7Metadata | None = None,
    version_fallback: str = "2.5.1",
    encoding: str = "utf-8",
) -> bytes:
    """Create an HL7 ACK that preserves MSH-10 when safely recoverable."""
    if code not in ACK_CODES:
        raise ValueError("ACK code must be AA, AE, or AR")
    if metadata is None:
        try:
            metadata = extract_msh_metadata(raw_message, encoding)
        except Exception:
            metadata = Hl7Metadata()
    trigger = _safe_field(metadata.trigger_event)
    control_id = _safe_field(metadata.message_control_id)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%z")
    message_type = f"ACK^{trigger}^ACK" if trigger else "ACK"
    fields = [
        "MSH",
        "^~\\&",
        _safe_field(metadata.receiving_application),
        _safe_field(metadata.receiving_facility),
        _safe_field(metadata.sending_application),
        _safe_field(metadata.sending_facility),
        timestamp,
        "",
        message_type,
        uuid4().hex,
        _safe_field(metadata.processing_id) or "P",
        _safe_field(metadata.version) or _safe_field(version_fallback),
    ]
    ack = "|".join(fields) + f"\rMSA|{code}|{control_id}\r"
    try:
        encoded = ack.encode(encoding, errors="strict")
    except (LookupError, UnicodeEncodeError):
        encoded = ack.encode("ascii", errors="replace")
    return MLLP_START + encoded + MLLP_END + MLLP_CR
