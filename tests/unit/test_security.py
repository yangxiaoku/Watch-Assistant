import pytest
from pwdlib import PasswordHash
from starlette.requests import Request

from tests.unit.factories import make_security_manager
from watch_assistant.security import (
    AGENT_SCOPES,
    AuthError,
    SecurityManager,
    _client_host,
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
        "/api/v1/libraries/library/strm-cleanup-plan",
        "/api/v1/strm-cleanup-plans/plan/apply",
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
        "/api/v1/libraries/library/strm-manifest",
        "/api/v1/strm-cleanup-plans/plan",
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


def test_backup_routes_require_backup_scopes():
    """M3:默认 token 不得读取备份内容,更不得触发全库备份。"""
    for path in ("/api/v1/backups", "/api/v1/backups/backup_1/restore-preview"):
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
        assert _required_scope(request) == "backup:read", path
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/backups",
            "headers": [],
            "scheme": "http",
            "server": ("app.test", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )
    assert _required_scope(request) == "backup:write"


def _login_request(
    *, client_host: str = "127.0.0.1", xff: str | None = None
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": headers,
            "scheme": "http",
            "server": ("app.test", 80),
            "client": (client_host, 1234),
            "root_path": "",
        }
    )


def _rate_limit_manager() -> SecurityManager:
    password_hash = PasswordHash.recommended()
    return SecurityManager(
        web_password_hash=password_hash.hash("fixture-password"),
        script_token_hash=password_hash.hash("fixture-token"),
    )


def test_login_rate_limit_bucket_blocks_sixth_attempt():
    """L4:同一客户端地址 1 分钟内最多 5 次登录尝试,第 6 次返回 429。"""
    manager = _rate_limit_manager()
    request = _login_request()
    for _ in range(5):
        manager.check_login_rate_limit(request)
    with pytest.raises(AuthError) as error:
        manager.check_login_rate_limit(request)
    assert error.value.status_code == 429


def test_login_rate_limit_bucket_is_per_client_address():
    """L4:限流桶按客户端地址隔离,不同地址互不牵连。"""
    manager = _rate_limit_manager()
    for _ in range(5):
        manager.check_login_rate_limit(_login_request(client_host="10.0.0.1"))
    # 另一个地址的客户端不受影响
    manager.check_login_rate_limit(_login_request(client_host="10.0.0.2"))
    with pytest.raises(AuthError) as error:
        manager.check_login_rate_limit(_login_request(client_host="10.0.0.1"))
    assert error.value.status_code == 429


def test_client_host_ignores_xff_unless_explicitly_trusted(monkeypatch):
    """L4:未配置 X_FORWARDED_FOR_TRUSTED 时忽略 XFF,伪造头无法绕过限流。"""
    monkeypatch.delenv("X_FORWARDED_FOR_TRUSTED", raising=False)
    request = _login_request(client_host="10.0.0.9", xff="203.0.113.7")
    assert _client_host(request) == "10.0.0.9"
    manager = _rate_limit_manager()
    for _ in range(5):
        manager.check_login_rate_limit(request)
    with pytest.raises(AuthError) as error:
        manager.check_login_rate_limit(request)
    assert error.value.status_code == 429


def test_client_host_prefers_trusted_xff_first_entry(monkeypatch):
    """L4:配置可信反代后,_client_host 取 XFF 首项作为真实客户端地址。"""
    monkeypatch.setenv("X_FORWARDED_FOR_TRUSTED", "1")
    request = _login_request(client_host="10.0.0.9", xff="203.0.113.7, 10.0.0.9")
    assert _client_host(request) == "203.0.113.7"


def test_trusted_xff_splits_rate_buckets_per_real_client(monkeypatch):
    """L4:可信反代下不同真实客户端(不同 XFF 首项)各自独立限流桶。"""
    monkeypatch.setenv("X_FORWARDED_FOR_TRUSTED", "1")
    manager = _rate_limit_manager()
    for _ in range(5):
        manager.check_login_rate_limit(
            _login_request(client_host="10.0.0.9", xff="203.0.113.7")
        )
    manager.check_login_rate_limit(
        _login_request(client_host="10.0.0.9", xff="203.0.113.8")
    )
    with pytest.raises(AuthError) as error:
        manager.check_login_rate_limit(
            _login_request(client_host="10.0.0.9", xff="203.0.113.7")
        )
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_require_api_auth_fails_closed_without_manager(tmp_path):
    """L6: a missing security manager must reject every request (fail-closed)."""
    from typing import Annotated

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from watch_assistant.security import require_api_auth

    app = FastAPI()

    @app.get("/probe")
    async def probe(_: Annotated[object, Depends(require_api_auth)]):
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/probe")
    assert response.status_code == 503
    assert response.json()["detail"] == "auth_unavailable"


def _noop_aclose():
    return None


_ALL_SCOPE_VALUES = frozenset(AGENT_SCOPES)


def _scope_request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "scheme": "http",
            "server": ("app.test", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


def test_unknown_api_path_is_fail_closed_not_default_scope():
    """M3 加固:未显式映射的 API 路径必须 fail-closed(拒绝),不得静默
    回退到默认 scope。否则任何未来新增写端点漏配清单都会降级为
    task:write,低权限 agent token 的实际边界取决于容易失配的前缀表。"""
    for method, path in (
        ("POST", "/api/v1/not-yet-registered"),
        ("POST", "/api/v1/future-endpoint/write"),
        ("GET", "/api/v1/future-endpoint"),
        ("DELETE", "/api/v1/unknown-resource/one"),
    ):
        with pytest.raises(AuthError) as error:
            _required_scope(_scope_request(method, path))
        assert error.value.status_code == 403, (method, path)


def test_organization_history_reads_require_organize_plan_scope():
    """organization-history 含目标路径等敏感详情,读权限必须收敛到
    organize:plan,不得落回默认 system:read。"""
    request = _scope_request("GET", "/api/v1/organization-history")
    assert _required_scope(request) == "organize:plan"


def test_workflow_write_routes_require_organize_execute_scope():
    """workflow 审批/取消/发现是组织写操作,不得落回默认 task:write。"""
    for path in (
        "/api/v1/workflows/workflow-1/approval",
        "/api/v1/workflows/workflow-1/cancel",
        "/api/v1/workflows/workflow-1/discovery",
        "/api/v1/workflows/cancel-batch",
    ):
        assert _required_scope(_scope_request("POST", path)) == "organize:execute"


def test_imports_and_webhook_writes_require_explicit_write_scope():
    """imports(手动导入)与 webhook 管理是写操作,不得落回默认 task:write。"""
    assert _required_scope(_scope_request("POST", "/api/v1/imports")) == "organize:execute"
    assert _required_scope(_scope_request("POST", "/api/v1/webhooks/webhook-1/test")) == "settings:write"
    assert _required_scope(_scope_request("DELETE", "/api/v1/webhooks/webhook-1")) == "settings:write"


@pytest.mark.asyncio
async def test_all_registered_api_routes_have_an_explicit_scope(tmp_path):
    """所有注册的 API 路由都必须被 _required_scope 显式映射,不得落入
    fail-closed 兜底(否则新增端点漏配会被静默拒绝)。这是对
    security._required_scope 前缀表的全量不变量校验。"""

    from cryptography.fernet import Fernet

    from watch_assistant.app import create_app
    from watch_assistant.crypto import SecretCrypto
    from watch_assistant.db import create_database, initialize_database

    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'scope-all-routes.db'}"
    )
    await initialize_database(database.engine)
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=type("T", (), {"aclose": lambda self: _noop_aclose()})(),
        pansou_client=type("P", (), {"aclose": lambda self: _noop_aclose()})(),
        security_manager=make_security_manager(),
        frontend_dir=tmp_path / "missing",
    )

    unmapped: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not path.startswith("/api/v1"):
            continue
        if not methods:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            request = _scope_request(method, path)
            try:
                scope = _required_scope(request)
                assert scope in _ALL_SCOPE_VALUES, f"非法 scope {scope!r}"
            except AuthError:
                unmapped.append(f"{method} {path}")
            except Exception as error:  # noqa: BLE001
                unmapped.append(f"{method} {path} ({type(error).__name__})")

    await database.engine.dispose()
    assert not unmapped, "以下路由未映射 scope:\n" + "\n".join(sorted(unmapped))
