"""Minimal stdio MCP transport forwarding to the authenticated HTTP adapter."""

from __future__ import annotations

import json
import os
import sys

import httpx


def _error_response(request_id: object, error_code: str, message_zh: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32000,
            "message": message_zh,
            "data": {
                "error_code": error_code,
                "title_zh": "自动化接口暂时不可用",
                "message_zh": message_zh,
                "suggestion_zh": "请检查服务状态后重试。",
                "retryable": True,
                "action": "retry",
            },
        },
    }


def _request_id(value: object) -> object:
    return value.get("id") if isinstance(value, dict) else None


def _forward(client: httpx.Client, base_url: str, request: dict[str, object], token: str) -> dict[str, object]:
    try:
        response = client.post(
            f"{base_url}/api/v1/mcp",
            json=request,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return _error_response(_request_id(request), "mcp_transport_error", "服务响应未完成，本次 MCP 调用未执行。")
    if not isinstance(result, dict):
        return _error_response(_request_id(request), "mcp_invalid_response", "服务响应格式无效，本次 MCP 调用未完成。")
    return result


def main() -> int:
    base_url = os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("MCP_BEARER_TOKEN", "")
    if not token:
        print("MCP_BEARER_TOKEN is required", file=sys.stderr)
        return 2
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    result = _error_response(None, "invalid_request", "MCP 请求必须是 JSON 对象。")
                else:
                    result = _forward(client, base_url, request, token)
            except (TypeError, ValueError):
                result = _error_response(None, "invalid_request", "MCP 请求不是有效的 JSON。")
            sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
