"""Read-only verification of the configured TgtoDrive HTTP contract."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "tgto-contract.json"
)
SAFE_PATH = re.compile(r"/api/[A-Za-z0-9._~/{}/-]+")
SAFE_QUERY = re.compile(r"[A-Za-z0-9._~{}=&-]+")
REFERENCE_PLACEHOLDER = "{remote_reference}"


@dataclass(frozen=True)
class ContractResult:
    login_ok: bool
    submit_supported: bool
    status_supported: bool
    uncertain_fallback: bool


def _disabled(login_ok: bool = False) -> ContractResult:
    return ContractResult(login_ok, False, False, False)


def _load_contract() -> dict[str, Any]:
    path = Path(os.environ.get("TGTO_CONTRACT_PATH", DEFAULT_CONTRACT_PATH))
    with path.open(encoding="utf-8") as contract_file:
        contract = json.load(contract_file)
    if not isinstance(contract, dict):
        raise TypeError("TgtoDrive contract root must be an object")
    return contract


def _safe_api_path(value: object, *, allow_query: bool = False) -> bool:
    if not isinstance(value, str) or "\\" in value or "://" in value:
        return False
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return False
    if not SAFE_PATH.fullmatch(parsed.path):
        return False
    if any(segment in {"", ".", ".."} for segment in parsed.path.split("/")[2:]):
        return False
    return not parsed.query or (
        allow_query and SAFE_QUERY.fullmatch(parsed.query) is not None
    )


def _valid_route(route: object, method: str, path_key: str) -> bool:
    if not isinstance(route, dict):
        return False
    path = route.get(path_key)
    return route.get("method") == method and _safe_api_path(path)


def _valid_submit(route: object) -> bool:
    if not _valid_route(route, "POST", "path"):
        return False
    reference_field = route.get("remote_reference_field")
    return isinstance(reference_field, str) and bool(reference_field.strip())


def _valid_status(route: object) -> bool:
    if not isinstance(route, dict) or route.get("method") != "GET":
        return False
    template = route.get("path_template")
    if not isinstance(template, str) or template.count(REFERENCE_PLACEHOLDER) != 1:
        return False
    if "{" in template.replace(REFERENCE_PLACEHOLDER, ""):
        return False
    if "}" in template.replace(REFERENCE_PLACEHOLDER, ""):
        return False
    return _safe_api_path(template, allow_query=True)


def _valid_check(check: object) -> bool:
    if not _valid_route(check, "GET", "path"):
        return False
    accepted = check.get("accept_status")
    return (
        isinstance(accepted, list)
        and bool(accepted)
        and all(type(status) is int and 100 <= status <= 599 for status in accepted)
    )


async def run_contract_check() -> ContractResult:
    """Verify login and safe GET checks without submitting a remote task."""
    try:
        contract = _load_contract()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _disabled()
    base_url = os.environ.get("TGTO_BASE_URL")
    username = os.environ.get("TGTO_WEB_USER")
    password = os.environ.get("TGTO_WEB_PASSWORD")
    login = contract.get("login")

    if not base_url or username is None or password is None:
        return _disabled()
    if not _valid_route(login, "POST", "path") or login["path"] != "/api/login":
        return _disabled()

    payload = {
        login.get("username_field", "username"): username,
        login.get("password_field", "password"): password,
    }
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.post(login["path"], json=payload)
            success_field = login.get("success_field", "success")
            response_data = response.json()
            login_ok = (
                response.is_success
                and isinstance(response_data, dict)
                and response_data.get(success_field) is True
            )
            if not login_ok:
                return _disabled()

            if contract.get("supported") is not True:
                return _disabled(login_ok=True)
            checks = contract.get("checks")
            if (
                not isinstance(checks, list)
                or not checks
                or not all(_valid_check(check) for check in checks)
            ):
                return _disabled(login_ok=True)
            if not _valid_submit(contract.get("submit")):
                return _disabled(login_ok=True)

            for check in checks:
                check_response = await client.get(check["path"])
                accepted = check["accept_status"]
                if check_response.status_code not in accepted:
                    return _disabled(login_ok=True)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return _disabled()

    submit_supported = True
    status_supported = _valid_status(contract.get("status"))
    return ContractResult(
        login_ok=True,
        submit_supported=submit_supported,
        status_supported=status_supported,
        uncertain_fallback=submit_supported and not status_supported,
    )
