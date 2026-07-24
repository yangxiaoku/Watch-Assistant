"""Read-only verification of the configured TgtoDrive HTTP contract."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "tgto-contract.json"
)


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
        return json.load(contract_file)


def _valid_route(route: object, method: str, path_key: str) -> bool:
    if not isinstance(route, dict):
        return False
    path = route.get(path_key)
    return route.get("method") == method and isinstance(path, str) and path.startswith(
        "/api/"
    )


async def run_contract_check() -> ContractResult:
    """Verify login and safe GET checks without submitting a remote task."""
    contract = _load_contract()
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
            login_ok = response.is_success and response.json().get(success_field) is True
            if not login_ok:
                return _disabled()

            checks_ok = True
            for check in contract.get("checks", []):
                if not _valid_route(check, "GET", "path"):
                    checks_ok = False
                    break
                check_response = await client.get(check["path"])
                accepted = check.get("accept_status", [200])
                if check_response.status_code not in accepted:
                    checks_ok = False
                    break
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return _disabled()

    contract_supported = contract.get("supported") is True and checks_ok
    submit_supported = contract_supported and _valid_route(
        contract.get("submit"), "POST", "path"
    )
    status_supported = contract_supported and _valid_route(
        contract.get("status"), "GET", "path_template"
    )
    return ContractResult(
        login_ok=True,
        submit_supported=submit_supported,
        status_supported=status_supported,
        uncertain_fallback=submit_supported and not status_supported,
    )
