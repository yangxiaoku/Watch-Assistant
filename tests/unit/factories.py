from datetime import UTC, datetime, timedelta

from watch_assistant.models import Task, TaskState
from watch_assistant.security import AuthContext, SecurityManager


def make_task(
    *,
    state: TaskState = TaskState.QUEUED,
    age_hours: int = 0,
    resource_id: str = "res_test",
) -> Task:
    created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    return Task(
        id=f"task_{state.value}_{age_hours}",
        resource_id=resource_id,
        action="offline_download",
        encrypted_url_snapshot="encrypted",
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )


class _PermissiveTestManager(SecurityManager):
    """Test-only manager that authenticates every request.

    Production stays fail-closed (a missing security manager rejects all
    requests). Tests inject this manager explicitly through the lite
    create_app path so route contracts can be exercised without a
    login/CSRF dance. It must never be constructed by application code.
    """

    def __init__(self) -> None:
        from pwdlib import PasswordHash

        super().__init__(
            web_password_hash=PasswordHash.recommended().hash("test-password"),
            script_token_hash=PasswordHash.recommended().hash("test-script-token"),
            cookie_secure=False,
        )

    async def authenticate_async(self, request):
        return AuthContext(
            identity="internal", via_bearer=True, csrf_token="test-csrf"
        )


def make_security_manager(
    *,
    web_password_hash: str | None = None,
    script_token_hash: str | None = None,
) -> SecurityManager:
    """Build a security manager for tests.

    Returns the permissive test manager by default so integration tests can
    exercise routes without credentials; passing explicit hashes builds a
    real manager (used by auth-focused tests).
    """
    if web_password_hash is not None or script_token_hash is not None:
        from pwdlib import PasswordHash

        return SecurityManager(
            web_password_hash=(
                web_password_hash
                if web_password_hash is not None
                else PasswordHash.recommended().hash("test-password")
            ),
            script_token_hash=(
                script_token_hash
                if script_token_hash is not None
                else PasswordHash.recommended().hash("test-script-token")
            ),
            cookie_secure=False,
        )
    return _PermissiveTestManager()
