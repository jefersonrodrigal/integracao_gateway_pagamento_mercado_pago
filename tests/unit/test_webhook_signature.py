import time

from app.integrations.mercadopago.webhooks import (
    build_manifest,
    compute_signature,
    parse_signature_header,
    verify_signature,
)

SECRET = "secret"


def _header(data_id: str | None, request_id: str | None, ts: str, secret: str = SECRET) -> str:
    return f"ts={ts},v1={compute_signature(secret, build_manifest(data_id, request_id, ts))}"


def test_manifest_follows_official_template_and_lowercases_alphanumeric_id() -> None:
    assert build_manifest("ORD01ABC", "req-1", "1700") == "id:ord01abc;request-id:req-1;ts:1700;"
    assert build_manifest(None, "req-1", "1700") == "request-id:req-1;ts:1700;"
    assert build_manifest("ORD1", None, "1700") == "id:ord1;ts:1700;"


def test_parse_header_tolerates_spaces_and_order() -> None:
    assert parse_signature_header("v1=abc, ts=123") == ("123", "abc")
    assert parse_signature_header("garbage") == (None, None)


def test_valid_signature() -> None:
    ts = str(int(time.time()))
    result = verify_signature(
        secret=SECRET,
        x_signature=_header("ORD1", "r1", ts),
        x_request_id="r1",
        data_id="ORD1",
        tolerance_seconds=300,
    )
    assert result.valid


def test_signature_with_millisecond_timestamp() -> None:
    ts = str(int(time.time() * 1000))
    result = verify_signature(
        secret=SECRET,
        x_signature=_header("ORD1", "r1", ts),
        x_request_id="r1",
        data_id="ORD1",
        tolerance_seconds=300,
    )
    assert result.valid


def test_rejects_wrong_secret_tampered_id_and_missing_header() -> None:
    ts = str(int(time.time()))
    common = {"x_request_id": "r1", "tolerance_seconds": 300}
    wrong = verify_signature(
        secret=SECRET, x_signature=_header("ORD1", "r1", ts, "other"), data_id="ORD1", **common
    )  # type: ignore[arg-type]
    tampered = verify_signature(
        secret=SECRET, x_signature=_header("ORD1", "r1", ts), data_id="ORD2", **common
    )  # type: ignore[arg-type]
    missing = verify_signature(secret=SECRET, x_signature=None, data_id="ORD1", **common)  # type: ignore[arg-type]
    malformed = verify_signature(secret=SECRET, x_signature="ts=abc,v1=", data_id="ORD1", **common)  # type: ignore[arg-type]
    assert (wrong.reason, tampered.reason) == ("signature_mismatch", "signature_mismatch")
    assert missing.reason == "missing_signature"
    assert malformed.reason == "malformed_signature"


def test_rejects_replayed_old_notification() -> None:
    ts = str(int(time.time()) - 3600)
    result = verify_signature(
        secret=SECRET,
        x_signature=_header("ORD1", "r1", ts),
        x_request_id="r1",
        data_id="ORD1",
        tolerance_seconds=300,
    )
    assert result.reason == "timestamp_out_of_tolerance"
