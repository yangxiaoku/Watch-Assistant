"""TgtoDrive adapter gated by the independently verified contract."""

from typing import Any
from urllib.parse import quote

import httpx

from watch_assistant.schemas import RemoteStatus, SubmissionResult


class TgtoUnsupported(RuntimeError):
    pass


class TgtoDriveClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        contract: dict[str, Any],
        *,
        timeout: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._contract = contract
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url)
        self._logged_in = False

    async def submit_magnet(self, url: str) -> SubmissionResult:
        return await self._submit(url, None)

    async def save_share(
        self, url: str, password: str | None
    ) -> SubmissionResult:
        return await self._submit(url, password)

    async def get_status(self, remote_ref: str) -> RemoteStatus | None:
        self._require_supported()
        status_route = self._contract.get("status")
        if not isinstance(status_route, dict):
            return None
        template = status_route.get("path_template")
        if not isinstance(template, str):
            return None
        login_status = await self._login_status()
        if login_status is not None:
            return login_status
        try:
            response = await self._client.get(
                template.replace(
                    "{remote_reference}", quote(remote_ref, safe="")
                ),
                timeout=self._timeout,
            )
            if response.status_code in {401, 403}:
                return RemoteStatus.NEEDS_AUTH
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(status_route.get("status_field", "status"))
        try:
            return RemoteStatus(str(value).casefold())
        except ValueError:
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _submit(
        self, url: str, password: str | None
    ) -> SubmissionResult:
        self._require_supported()
        login_status = await self._login_status()
        if login_status is not None:
            return SubmissionResult(
                status=login_status,
                error_code="login_unavailable"
                if login_status == RemoteStatus.UNCERTAIN
                else None,
                error_message="login outcome is uncertain"
                if login_status == RemoteStatus.UNCERTAIN
                else None,
            )
        route = self._contract["submit"]
        payload = {route.get("url_field", "url"): url}
        if password is not None:
            payload[route.get("password_field", "password")] = password
        try:
            response = await self._client.post(
                route["path"], json=payload, timeout=self._timeout
            )
            if response.status_code in {401, 403}:
                return SubmissionResult(status=RemoteStatus.NEEDS_AUTH)
            if 400 <= response.status_code < 500:
                return SubmissionResult(
                    status=RemoteStatus.FAILED,
                    error_code="submit_rejected",
                    error_message="remote service rejected the submission",
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return SubmissionResult(
                status=RemoteStatus.UNCERTAIN,
                error_code="submit_ambiguous",
                error_message="submission outcome is uncertain",
            )
        if not isinstance(body, dict):
            return SubmissionResult(
                status=RemoteStatus.UNCERTAIN,
                error_code="missing_remote_reference",
                error_message="remote reference was not returned",
            )
        remote_ref = body.get(route.get("remote_reference_field", "id"))
        if not isinstance(remote_ref, (str, int)):
            return SubmissionResult(
                status=RemoteStatus.UNCERTAIN,
                error_code="missing_remote_reference",
                error_message="remote reference was not returned",
            )
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref=str(remote_ref))

    async def _login_status(self) -> RemoteStatus | None:
        if self._logged_in:
            return None
        login = self._contract.get("login")
        if not isinstance(login, dict) or login.get("path") != "/api/login":
            return RemoteStatus.NEEDS_AUTH
        payload = {
            login.get("username_field", "username"): self._username,
            login.get("password_field", "password"): self._password,
        }
        try:
            response = await self._client.post(
                login["path"], json=payload, timeout=self._timeout
            )
            if response.status_code in {401, 403}:
                return RemoteStatus.NEEDS_AUTH
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return RemoteStatus.UNCERTAIN
        self._logged_in = isinstance(body, dict) and body.get(
            login.get("success_field", "success")
        ) is True
        return None if self._logged_in else RemoteStatus.NEEDS_AUTH

    def _require_supported(self) -> None:
        login = self._contract.get("login")
        submit = self._contract.get("submit")
        status = self._contract.get("status")
        valid = (
            self._contract.get("supported") is True
            and isinstance(login, dict)
            and login.get("method") == "POST"
            and login.get("path") == "/api/login"
            and isinstance(submit, dict)
            and submit.get("method") == "POST"
            and _safe_api_path(submit.get("path"))
            and isinstance(submit.get("remote_reference_field"), str)
            and (
                status is None
                or (
                    isinstance(status, dict)
                    and status.get("method") == "GET"
                    and _safe_api_path(status.get("path_template"))
                    and "{remote_reference}" in status["path_template"]
                )
            )
        )
        if not valid:
            raise TgtoUnsupported("TgtoDrive submit contract is unsupported")


def _safe_api_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/api/")
        and "://" not in value
        and "\\" not in value
        and ".." not in value.split("/")
    )
