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


def test_admin_bootstrap_rejects_a_configured_password_hash():
    password_hash = PasswordHash.recommended()

    with pytest.raises(ValueError, match="web_password_hash"):
        SecurityManager(
            web_password_hash=password_hash.hash("fixture-password"),
            script_token_hash=password_hash.hash("fixture-token"),
            bootstrap_admin_enabled=True,
            bootstrap_admin_password="bootstrap-fixture-password",
        )


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


@pytest.mark.parametrize(
    ("path", "expected_scope"),
    [
        ("/api/v1/organization-plans/plan/confirm", "organize:plan"),
        ("/api/v1/organization-plans/plan/candidate", "organize:plan"),
        ("/api/v1/organization-plans/plan/operation", "organize:execute"),
        (
            "/api/v1/organization-plans/plan/confirm-and-operation",
            "organize:execute",
        ),
        ("/api/v1/organization-operations/batch", "organize:execute"),
        (
            "/api/v1/organization-operations/confirm-and-batch",
            "organize:execute",
        ),
    ],
)
def test_organization_plan_routes_require_plan_or_execute_scope(
    path: str, expected_scope: str
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
    assert _required_scope(request) == expected_scope


def test_library_write_routes_require_library_write_scope():
    """H1:只读 agent token 不得触发全库扫描/取消/计划等写操作。"""
    for path in (
        "/api/v1/libraries/library/scan",
        "/api/v1/libraries/library/scans/scan-1/cancel",
        "/api/v1/libraries/library/empty-directory-cleanup-plan",
        "/api/v1/libraries/library/organization-preview",
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
        assert _required_scope(request) == "library:write", path


def test_library_read_routes_keep_library_read_scope():
    for path in (
        "/api/v1/libraries",
        "/api/v1/libraries/library",
        "/api/v1/libraries/library/inventory",
        "/api/v1/media",
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
        assert _required_scope(request) == "library:read", path
