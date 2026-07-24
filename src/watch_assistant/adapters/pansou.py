"""PanSou search and share-link validation adapter."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx


class PanSouError(RuntimeError):
    pass


class LinkCheckState(StrEnum):
    OK = "ok"
    BAD = "bad"
    LOCKED = "locked"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class LinkCheckItem:
    url: str
    password: str | None


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

    async def check_links(
        self,
        items: list[LinkCheckItem],
        *,
        batch_size: int = 10,
    ) -> list[LinkCheckState]:
        states: list[LinkCheckState] = []
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            payload = {
                "items": [
                    {
                        "disk_type": "115",
                        "url": item.url,
                        **(
                            {"password": item.password}
                            if item.password is not None
                            else {}
                        ),
                    }
                    for item in batch
                ]
            }
            try:
                response = await self._client.post(
                    "/api/check/links",
                    json=payload,
                    timeout=max(self._timeout, len(batch) * 12.0),
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise PanSouError("PanSou link check failed") from exc
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise PanSouError("Unexpected PanSou link check response shape")
            if not all(isinstance(result, dict) for result in results):
                raise PanSouError("Unexpected PanSou link check response shape")
            try:
                states.extend(
                    LinkCheckState(result["state"])
                    for result in results
                )
            except (KeyError, ValueError) as exc:
                raise PanSouError(
                    "Unexpected PanSou link check response shape"
                ) from exc
            if len(states) != offset + len(batch):
                raise PanSouError("Unexpected PanSou link check response shape")
        return states

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
