"""Version-aware HL7 v2 ER7 parser with ordered, loss-conscious JSON output."""

import re
from dataclasses import dataclass
from typing import Any

from hl7apy.parser import parse_message

from healthcare_platform.integrations.hl7.models import (
    Hl7MessageError,
    Hl7Metadata,
    ParsedHl7Message,
)

SUPPORTED_MESSAGE_TYPES = {
    "ADT": frozenset({"A01", "A02", "A03", "A04", "A08", "A40"}),
    "ORM": frozenset({"O01"}),
    "ORU": frozenset({"R01"}),
}
_CONTROL_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SEGMENT_NAME = re.compile(r"[A-Z0-9]{3}\Z")


@dataclass(frozen=True)
class _Segment:
    name: str
    raw: str
    fields: tuple[str, ...]

    def field(self, position: int) -> str:
        if position < 1 or position > len(self.fields):
            return ""
        return self.fields[position - 1]


def _metadata_from_msh(segment: str, field_separator: str) -> Hl7Metadata:
    parts = segment.split(field_separator)
    if len(parts) < 12 or parts[0] != "MSH":
        raise Hl7MessageError("invalid_msh", "HL7 message has an incomplete MSH segment")
    encoding_chars = parts[1]
    if (
        len(encoding_chars) != 4
        or len(set(encoding_chars)) != 4
        or field_separator in encoding_chars
        or any(ord(char) < 33 for char in encoding_chars)
    ):
        raise Hl7MessageError("invalid_encoding", "HL7 MSH encoding characters are invalid")
    message_parts = parts[8].split(encoding_chars[0])
    if len(message_parts) < 2 or not message_parts[0] or not message_parts[1]:
        raise Hl7MessageError("invalid_message_type", "HL7 MSH-9 message type is invalid")
    metadata = Hl7Metadata(
        sending_application=parts[2],
        sending_facility=parts[3],
        receiving_application=parts[4],
        receiving_facility=parts[5],
        message_timestamp=parts[6] or None,
        message_code=message_parts[0],
        trigger_event=message_parts[1],
        message_structure=message_parts[2] if len(message_parts) > 2 else None,
        message_control_id=parts[9] or None,
        processing_id=parts[10] or None,
        version=parts[11] or None,
    )
    return metadata


def extract_msh_metadata(raw: bytes, encoding: str = "utf-8") -> Hl7Metadata:
    """Extract MSH fields for ACK/audit use without returning source values in errors."""
    try:
        text = raw.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise Hl7MessageError("invalid_encoding", "HL7 message encoding is invalid") from exc
    msh = text.split("\r", maxsplit=1)[0]
    if len(msh) < 4 or not msh.startswith("MSH"):
        raise Hl7MessageError("invalid_msh", "HL7 message must start with MSH")
    return _metadata_from_msh(msh, msh[3])


def _split_composite(
    value: str,
    component_separator: str,
    subcomponent_separator: str,
    escape_character: str,
    field_separator: str,
    repetition_separator: str,
) -> list[dict[str, Any]]:
    escapes = {
        "F": field_separator,
        "S": component_separator,
        "R": repetition_separator,
        "T": subcomponent_separator,
        "E": escape_character,
    }

    def decode(text: str) -> str:
        for code, delimiter in escapes.items():
            text = text.replace(f"{escape_character}{code}{escape_character}", delimiter)
        return text

    components: list[dict[str, Any]] = []
    for component in value.split(component_separator):
        components.append(
            {
                "value": component,
                "decoded_value": decode(component),
                "subcomponents": component.split(subcomponent_separator),
            }
        )
    return components


def _serialize_segment(
    segment: _Segment,
    repetition_separator: str,
    component_separator: str,
    subcomponent_separator: str,
    escape_character: str,
    field_separator: str,
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for position, raw_value in enumerate(segment.fields, start=1):
        repetitions = [
            {
                "components": _split_composite(
                    value,
                    component_separator,
                    subcomponent_separator,
                    escape_character,
                    field_separator,
                    repetition_separator,
                )
            }
            for value in raw_value.split(repetition_separator)
        ]
        fields.append({"position": position, "repetitions": repetitions})
    return {"name": segment.name, "raw": segment.raw, "fields": fields}


def _first_segment(segments: list[_Segment], name: str) -> _Segment | None:
    return next((segment for segment in segments if segment.name == name), None)


def _all_segments(segments: list[_Segment], name: str) -> list[_Segment]:
    return [segment for segment in segments if segment.name == name]


def _identifiers(
    segment: _Segment | None,
    position: int,
    component_separator: str = "^",
    repetition_separator: str = "~",
) -> list[dict[str, Any]]:
    if segment is None or not segment.field(position):
        return []
    return [
        {"value": value, "components": value.split(component_separator)}
        for value in segment.field(position).split(repetition_separator)
    ]


def _normalize(
    segments: list[_Segment],
    category: str,
    trigger: str,
    component_separator: str,
    repetition_separator: str,
) -> dict[str, Any]:
    def ids(segment: _Segment | None, position: int) -> list[dict[str, Any]]:
        return _identifiers(segment, position, component_separator, repetition_separator)

    pid = _first_segment(segments, "PID")
    pv1 = _first_segment(segments, "PV1")
    patient: dict[str, Any] = {
        "identifiers": ids(pid, 3),
        "names": ids(pid, 5),
        "date_of_birth": pid.field(7) or None if pid else None,
        "administrative_sex": pid.field(8) or None if pid else None,
    }
    normalized: dict[str, Any] = {
        "category": category,
        "trigger_event": trigger,
        "patient": patient,
        "visit": {
            "patient_class": pv1.field(2) or None if pv1 else None,
            "location": ids(pv1, 3),
            "visit_number": pv1.field(19) or None if pv1 else None,
        },
        "next_of_kin": [
            {"set_id": segment.field(1) or None, "name": ids(segment, 2)}
            for segment in _all_segments(segments, "NK1")
        ],
    }
    if category == "ADT":
        evn = _first_segment(segments, "EVN")
        mrg = _first_segment(segments, "MRG")
        normalized["event"] = {
            "event_type": evn.field(1) or trigger if evn else trigger,
            "recorded_at": evn.field(2) or None if evn else None,
            "planned_at": evn.field(3) or None if evn else None,
        }
        if mrg:
            normalized["merge"] = {"prior_patient_identifiers": ids(mrg, 1)}
    if category in {"ORM", "ORU"}:
        orders: list[dict[str, Any]] = []
        current_order: dict[str, Any] = {}
        for segment in segments:
            if segment.name == "ORC":
                current_order = {
                    "control_code": segment.field(1) or None,
                    "placer_order_number": segment.field(2) or None,
                    "filler_order_number": segment.field(3) or None,
                    "status": segment.field(5) or None,
                    "transaction_time": segment.field(9) or None,
                }
            elif segment.name == "OBR":
                current_order = {
                    **current_order,
                    "placer_order_number": current_order.get("placer_order_number")
                    or segment.field(2)
                    or None,
                    "filler_order_number": current_order.get("filler_order_number")
                    or segment.field(3)
                    or None,
                    "service": ids(segment, 4),
                    "observation_time": segment.field(7) or None,
                    "observations": [],
                }
                orders.append(current_order)
        normalized["orders"] = orders
    if category == "ORU":
        observations = [
            {
                "set_id": segment.field(1) or None,
                "value_type": segment.field(2) or None,
                "identifier": ids(segment, 3),
                "value": segment.field(5) or None,
                "value_repetitions": (
                    segment.field(5).split(repetition_separator) if segment.field(5) else []
                ),
                "units": ids(segment, 6),
                "reference_range": segment.field(7) or None,
                "status": segment.field(11) or None,
                "result_time": segment.field(14) or None,
            }
            for segment in _all_segments(segments, "OBX")
        ]
        normalized["observations"] = observations
        order_index = -1
        observation_index = 0
        for segment in segments:
            if segment.name == "OBR":
                order_index += 1
            elif segment.name == "OBX" and 0 <= order_index < len(orders):
                orders[order_index]["observations"].append(observations[observation_index])
                observation_index += 1
    return normalized


def parse_hl7_message(
    raw: bytes,
    *,
    supported_versions: tuple[str, ...] = ("2.5.1",),
    encoding: str = "utf-8",
) -> ParsedHl7Message:
    """Parse a complete MLLP payload and preserve all segments and generic structures."""
    metadata = extract_msh_metadata(raw, encoding)
    if metadata.version not in supported_versions:
        raise Hl7MessageError(
            "unsupported_version",
            "HL7 message version is not supported",
            ack_code="AR",
            metadata=metadata,
        )
    if (
        not metadata.processing_id
        or not metadata.message_control_id
        or not _CONTROL_ID.fullmatch(metadata.message_control_id)
        or not metadata.version
    ):
        raise Hl7MessageError(
            "missing_msh_field",
            "HL7 MSH is missing a required header field",
            metadata=metadata,
        )

    category = metadata.message_code or ""
    trigger = metadata.trigger_event or ""
    if trigger not in SUPPORTED_MESSAGE_TYPES.get(category, frozenset()):
        raise Hl7MessageError(
            "unsupported_message_type",
            "HL7 message type is not supported",
            ack_code="AR",
            metadata=metadata,
        )
    try:
        text = raw.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise Hl7MessageError(
            "invalid_encoding", "HL7 message encoding is invalid", metadata=metadata
        ) from exc

    try:
        # The standards parser checks version-specific message grammar and field structure.
        parse_message(text, validation_level=1, find_groups=True)
    except Exception as exc:
        raise Hl7MessageError(
            "invalid_message", "HL7 message could not be parsed", metadata=metadata
        ) from exc

    msh = text.split("\r", maxsplit=1)[0]
    field_separator = msh[3]
    encoding_chars = msh.split(field_separator)[1]
    component_separator, repetition_separator, escape_character, subcomponent_separator = (
        encoding_chars
    )
    lines = text.split("\r")
    if lines and lines[-1] == "":
        lines.pop()
    segments: list[_Segment] = []
    for line in lines:
        if not line:
            raise Hl7MessageError(
                "empty_segment", "HL7 message contains an empty segment", metadata=metadata
            )
        parts = line.split(field_separator)
        name = parts[0]
        if not _SEGMENT_NAME.fullmatch(name):
            raise Hl7MessageError(
                "invalid_segment", "HL7 message contains an invalid segment", metadata=metadata
            )
        fields = [field_separator, *parts[1:]] if name == "MSH" else parts[1:]
        segments.append(_Segment(name=name, raw=line, fields=tuple(fields)))

    names = {segment.name for segment in segments}
    required = {"MSH", "PID"}
    if category == "ADT":
        required.add("EVN")
        if trigger == "A40":
            required.add("MRG")
    elif category == "ORM":
        required.update({"ORC", "OBR"})
    elif category == "ORU":
        required.update({"OBR", "OBX"})
    if not required.issubset(names):
        missing_merge_segment = category == "ADT" and trigger == "A40" and "MRG" not in names
        raise Hl7MessageError(
            "missing_required_segment",
            "HL7 message is missing a required segment",
            ack_code="AR" if missing_merge_segment else "AE",
            metadata=metadata,
        )
    pid = _first_segment(segments, "PID")
    if pid is None or not pid.field(3):
        raise Hl7MessageError(
            "missing_patient_identifier",
            "HL7 message is missing a patient identifier",
            metadata=metadata,
        )

    serialized_segments = [
        _serialize_segment(
            segment,
            repetition_separator,
            component_separator,
            subcomponent_separator,
            escape_character,
            field_separator,
        )
        for segment in segments
    ]
    return ParsedHl7Message(
        metadata=metadata,
        segments=serialized_segments,
        normalized=_normalize(
            segments,
            category,
            trigger,
            component_separator,
            repetition_separator,
        ),
    )
