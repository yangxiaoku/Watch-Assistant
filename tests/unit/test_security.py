import pytest
from pwdlib import PasswordHash
from starlette.requests import Request

from watch_assistant.security import (
    AuthError,
    SecurityManager,
    _required_scope,
    redact_mapping,
)


def test_explicit_empty_username_is_not_treated_as_password_only_login():
    password_hash = PasswordHash.recommended()
    manager = SecurityManager(
        web_password_hash=password_hash.hash("fixture-password"),
        script_token_hash=password_hash.hash("fixture-token"),
    )

    with pytest.raises(AuthError) as error:
        manager.login("fixture-password", username="")

    assert error.value.status_code == 401


def test_structured_log_redaction_removes_sensitive_keys_and_values():
    data = {
        "url": "magnet:?xt=urn:btih:secret",
        "password": "share-password",
        "authorization": "Bearer script-token",
        "message": "failed for tmdb-key and script-token",
        "safe": "visible",
    }

    redacted = redact_mapping(data, secrets=("tmdb-key", "script-token"))

    rendered = repr(redacted)
    assert "magnet:?" not in rendered
    assert "share-password" not in rendered
    assert "script-token" not in rendered
    assert "tmdb-key" not in rendered
    assert redacted["safe"] == "visible"


def test_strm_write_routes_require_strm_write_scope():
    for path in (
        "/api/v1/libraries/library/strm-generation",
        "/api/v1/libraries/library/strm-incremental",
        "/api/v1/libraries/library/strm-cleanup",
    ):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "scheme": "http",
                "server": ("app.test", 80),
                "client": ("127.0.0.1", 1234),
                "root_path": "",
            }
        )
        assert _required_scope(request) == "strm:write"


def test_strm_operation_read_routes_require_strm_read_scope():
    for path in (
        "/api/v1/strm-operations/strm_op_1",
        "/api/v1/libraries/library/strm-operations",
    ):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],
                "scheme": "http",
                "server": ("app.test", 80),
                "client": ("127.0.0.1", 1234),
                "root_path": "",
            }
        )
        assert _required_scope(request) == "strm:read"
