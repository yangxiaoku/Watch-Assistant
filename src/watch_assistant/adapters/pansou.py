"""PanSou search adapter."""

from typing import Any

import httpx


class PanSouError(RuntimeError):
    pass


class PanSouClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))

    async def search(self, keyword: str) -> dict[str, Any]:
        try:
            response = await self._client.get(
                "/api/search", params={"kw": keyword}, timeout=self._timeout
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PanSouError("PanSou request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PanSouError("Unexpected PanSou response shape") from exc

        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise PanSouError("Unexpected PanSou response shape")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PanSouError("Unexpected PanSou response shape")
        merged_by_type = data.get("merged_by_type")
        if merged_by_type is None and data.get("total") == 0:
            merged_by_type = {}
        if not isinstance(merged_by_type, dict):
            raise PanSouError("Unexpected PanSou response shape")
        if not all(
            isinstance(items, list) for items in merged_by_type.values()
        ):
            raise PanSouError("Unexpected PanSou response shape")
        return {**data, "merged_by_type": merged_by_type}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
