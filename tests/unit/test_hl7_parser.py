import pytest

from healthcare_platform.integrations.hl7.ack import build_ack
from healthcare_platform.integrations.hl7.models import Hl7MessageError
from healthcare_platform.integrations.hl7.parser import parse_hl7_message

ADT = (
    b"MSH|^~\\&|APP|FAC|REC|RFAC|20250101120000||ADT^A01^ADT_A01|ctl-1|P|2.5.1\r"
    b"EVN|A01|20250101120000\r"
    b"PID|1||12345^^^FAC^MR||Synthetic^Patient||19800101|F\r"
    b"PV1|1|I\rNK1|1|Synthetic^Contact\rZXY|one|two\r"
)
ORM = (
    b"MSH|^~\\&|APP|FAC|REC|RFAC|20250101120000||ORM^O01|ctl-2|P|2.5.1\r"
    b"PID|1||12345^^^FAC^MR\rORC|NW|P1|F1\rOBR|1|P1|F1|CBC^CBC\r"
)
ORU = (
    b"MSH|^~\\&|APP|FAC|REC|RFAC|20250101120000||ORU^R01|ctl-3|P|2.5.1\r"
    b"PID|1||12345^^^FAC^MR\rORC|RE|P1|F1\rOBR|1|P1|F1|CBC^CBC\r"
    b"OBX|1|NM|WBC^White count||4.2|10*9/L|4-10||||F|||20250101120000\r"
)


def test_parses_adt_ordered_segments_and_unknown_z_segment() -> None:
    parsed = parse_hl7_message(ADT)
    assert parsed.metadata.message_control_id == "ctl-1"
    assert parsed.metadata.version == "2.5.1"
    assert [segment["name"] for segment in parsed.segments][-2:] == ["NK1", "ZXY"]
    assert parsed.normalized["patient"]["identifiers"][0]["components"][0] == "12345"


def test_parses_orm_orders() -> None:
    parsed = parse_hl7_message(ORM)
    assert parsed.normalized["orders"][0]["placer_order_number"] == "P1"
    assert parsed.normalized["orders"][0]["service"][0]["components"] == ["CBC", "CBC"]


def test_preserves_and_decodes_hl7_escape_sequences() -> None:
    message = ADT.replace(b"Synthetic^Patient", b"Synthetic\\S\\Patient")
    parsed = parse_hl7_message(message)
    name = parsed.segments[2]["fields"][4]["repetitions"][0]["components"][0]
    assert name["value"] == r"Synthetic\S\Patient"
    assert name["decoded_value"] == "Synthetic^Patient"


def test_parses_oru_results() -> None:
    parsed = parse_hl7_message(ORU)
    assert parsed.normalized["observations"][0]["value"] == "4.2"
    assert parsed.normalized["observations"][0]["status"] == "F"
    assert len(parsed.normalized["orders"][0]["observations"]) == 1


def test_rejects_unsupported_type_and_version() -> None:
    unsupported = ADT.replace(b"ADT^A01^ADT_A01", b"ADT^A99^ADT_A99")
    try:
        parse_hl7_message(unsupported)
    except Hl7MessageError as exc:
        assert exc.ack_code == "AR"
    else:
        raise AssertionError("unsupported event must be rejected")

    unsupported_version = ADT.replace(b"2.5.1", b"2.3")
    try:
        parse_hl7_message(unsupported_version)
    except Hl7MessageError as exc:
        assert exc.code == "unsupported_version"
    else:
        raise AssertionError("unsupported version must be rejected")


def test_rejects_missing_patient_identifier_without_echoing_payload() -> None:
    malformed = ADT.replace(b"12345^^^FAC^MR", b"")
    try:
        parse_hl7_message(malformed)
    except Hl7MessageError as exc:
        assert exc.code == "missing_patient_identifier"
        assert "Synthetic" not in str(exc)
    else:
        raise AssertionError("missing PID-3 must be rejected")


def test_optional_sender_and_receiver_fields_do_not_reject_valid_message() -> None:
    message = ADT.replace(
        b"|APP|FAC|REC|RFAC|",
        b"|||||",
    )
    parsed = parse_hl7_message(message)
    assert parsed.metadata.sending_application == ""
    assert parsed.metadata.sending_facility == ""


def _adt_event(trigger: str, extra_segments: bytes = b"") -> bytes:
    message_structure = {
        "A40": "ADT_A39",
        "A04": "ADT_A01",
        "A08": "ADT_A01",
    }.get(trigger, f"ADT_{trigger}")
    return (
        f"MSH|^~\\&|APP|FAC|REC|RFAC|20250101120000||ADT^{trigger}^{message_structure}|"
        f"control-{trigger}|P|2.5.1\r"
        f"EVN|{trigger}|20250101120000\r"
        "PID|1||CURRENT^^^FAC^MR||Synthetic^Person\r"
        "PV1|1|I\r"
    ).encode() + extra_segments


@pytest.mark.parametrize("trigger", ["A02", "A03", "A04", "A08"])
def test_parses_supported_adt_events(trigger: str) -> None:
    parsed = parse_hl7_message(_adt_event(trigger))
    assert parsed.metadata.trigger_event == trigger
    assert parsed.normalized["event"]["event_type"] == trigger


def test_adt_a40_extracts_repeated_prior_identifiers() -> None:
    message = _adt_event(
        "A40",
        b"MRG|OLD-1^^^FAC^MR~OLD-2^^^FAC^PI\r",
    )
    parsed = parse_hl7_message(message)
    identifiers = parsed.normalized["merge"]["prior_patient_identifiers"]
    assert len(identifiers) == 2
    assert identifiers[0]["components"] == ["OLD-1", "", "", "FAC", "MR"]
    assert identifiers[1]["components"] == ["OLD-2", "", "", "FAC", "PI"]


def test_adt_a40_requires_mrg_and_uses_reject_ack() -> None:
    with pytest.raises(Hl7MessageError) as error:
        parse_hl7_message(_adt_event("A40"))
    assert error.value.code == "missing_required_segment"
    assert error.value.ack_code == "AR"
    ack = build_ack(_adt_event("A40"), error.value.ack_code)
    assert b"MSA|AR|control-A40" in ack
