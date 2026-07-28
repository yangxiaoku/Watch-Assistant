from copy import deepcopy

import pytest

from watch_assistant.adapters.p115_c03_ipad_login import (
    C03QrcodeToken,
    parse_qrcode_token_response,
)


def _token_response(**overrides):
    response = {
        "data": {
            "uid": "sensitive-uid",
            "time": 123456,
            "sign": "sensitive-sign",
            "qrcode": "sensitive-qr-payload",
            "unknown": "must-not-survive",
        },
        "unknown": "must-not-survive",
    }
    response.update(overrides)
    return response


@pytest.mark.parametrize("state", [True, 1])
def test_token_parser_accepts_fixed_client_success_flags(state):
    token = parse_qrcode_token_response(_token_response(state=state, code=0))

    assert token is not None
    assert token.scan_payload() == {
        "uid": "sensitive-uid",
        "time": 123456,
        "sign": "sensitive-sign",
    }
    assert token.qr_payload() == "sensitive-qr-payload"


def test_token_parser_accepts_missing_state_and_qrcode_without_mutation():
    response = _token_response()
    response["data"].pop("qrcode")
    original = deepcopy(response)

    token = parse_qrcode_token_response(response)

    assert token is not None
    assert token.qr_payload().endswith("sensitive-uid")
    assert response == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": False},
        {"state": 0},
        {"state": "1"},
        {"success": False},
        {"state": True, "code": 1},
        {"state": True, "errno": "99"},
    ],
)
def test_token_parser_rejects_failed_or_contradictory_status(overrides):
    assert parse_qrcode_token_response(_token_response(**overrides)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uid", None),
        ("uid", ""),
        ("uid", "unsafe\nuid"),
        ("time", True),
        ("time", "123456"),
        ("time", -1),
        ("sign", None),
        ("sign", "unsafe\x00sign"),
        ("qrcode", []),
    ],
)
def test_token_parser_rejects_missing_or_malformed_token_fields(field, value):
    response = _token_response()
    response["data"][field] = value

    assert parse_qrcode_token_response(response) is None


def test_token_dto_repr_redacts_all_authentication_fields():
    token = C03QrcodeToken(
        "sensitive-uid", 123456, "sensitive-sign", "sensitive-qr-payload"
    )

    rendered = repr(token)
    assert "sensitive" not in rendered
    assert "123456" not in rendered
    assert rendered == (
        "C03QrcodeToken(uid_present=True, sign_present=True, qrcode_present=True)"
    )
