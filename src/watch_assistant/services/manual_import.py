"""Safe, preview-first manual resource import."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.crypto import SecretCrypto
from watch_assistant.models import Resource
from watch_assistant.schemas import (
    ManualImportPreviewResponse,
    ManualImportRequest,
    ManualImportResponse,
)
from watch_assistant.services.normalize import normalize_pansou
from watch_assistant.services.search import SearchService
from watch_assistant.services.validation import resource_matches_media

IMPORT_RESOURCE_TTL = timedelta(days=30)


class ManualImportError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ManualImportService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        search_service: SearchService,
        crypto: SecretCrypto,
        *,
        share_domains: tuple[str, ...] = ("115.com", "115cdn.com"),
    ) -> None:
        self._session_factory = session_factory
        self._search_service = search_service
        self._crypto = crypto
        self._share_domains = share_domains

    async def preview(
        self, request: ManualImportRequest
    ) -> ManualImportPreviewResponse:
        normalized = await self._normalize(request)
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(Resource).where(
                    Resource.canonical_key == normalized.canonical_key
                )
            )
        if existing is not None:
            return _preview(
                request,
                normalized,
                status="duplicate",
                resource_id=existing.id,
                warnings=["resource_already_exists"],
            )
        return _preview(request, normalized, status="ready")

    async def confirm(self, request: ManualImportRequest) -> ManualImportResponse:
        if not request.confirmed:
            raise ManualImportError("confirmation_required")
        normalized = await self._normalize(request)
        now = datetime.now(UTC)
        resource_id = "res_" + sha256(normalized.canonical_key.encode()).hexdigest()[:24]
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(Resource).where(
                    Resource.canonical_key == normalized.canonical_key
                )
            )
            if existing is not None:
                return _response(existing.id, existing.kind, existing.name, "duplicate")
            session.add(
                Resource(
                    id=resource_id,
                    kind=normalized.kind,
                    canonical_key=normalized.canonical_key,
                    encrypted_url=self._crypto.encrypt(normalized.url),
                    encrypted_password=(
                        self._crypto.encrypt(normalized.password)
                        if normalized.password
                        else None
                    ),
                    name=normalized.name,
                    source="manual",
                    captured_at=now,
                    expires_at=now + IMPORT_RESOURCE_TTL,
                    metadata_json=_metadata_json(request),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(Resource).where(
                        Resource.canonical_key == normalized.canonical_key
                    )
                )
                if existing is None:
                    raise ManualImportError("resource_conflict") from None
                return _response(existing.id, existing.kind, existing.name, "duplicate")
        return _response(resource_id, normalized.kind, normalized.name, "created")

    async def _normalize(self, request: ManualImportRequest):
        _validate_url(request.url, self._share_domains)
        parsed = urlsplit(request.url)
        item = {
            "url": request.url,
            "note": request.name or "Manual resource",
            "source": "manual",
        }
        if request.password is not None:
            item["password"] = request.password.get_secret_value()
        category = "magnet" if parsed.scheme.casefold() == "magnet" else "115"
        normalized = normalize_pansou(
            {"merged_by_type": {category: [item]}},
            share_domains=self._share_domains,
            captured_at=datetime.now(UTC),
        )
        if len(normalized) != 1:
            raise ManualImportError("invalid_resource")
        resource = normalized[0]
        media = await self._search_service.get_media(request.tmdb_id, request.media_type)
        if not resource_matches_media(
            media,
            resource.name,
            season_number=request.season_number,
        ):
            raise ManualImportError("media_mismatch")
        return resource


def _validate_url(url: str, share_domains: tuple[str, ...]) -> None:
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() == "magnet":
        if parsed.netloc or parsed.path or parsed.fragment or not parsed.query:
            raise ManualImportError("invalid_magnet")
        return
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ManualImportError("unsupported_url")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManualImportError("invalid_share_url") from exc
    if parsed.username or parsed.password or port not in (None, 80, 443):
        raise ManualImportError("invalid_share_url")
    host = parsed.hostname.casefold().rstrip(".").removeprefix("www.")
    allowed = {
        domain.casefold().rstrip(".").removeprefix("www.")
        for domain in share_domains
    }
    if host not in allowed or not parsed.path.startswith(("/s/", "/share/")):
        raise ManualImportError("unsupported_share_domain")


def _metadata_json(request: ManualImportRequest) -> str:
    import json

    return json.dumps(
        {
            "manual_import": True,
            "tmdb_id": request.tmdb_id,
            "media_type": request.media_type.value,
            "season_number": request.season_number,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _preview(request, resource, *, status, resource_id=None, warnings=None):
    return ManualImportPreviewResponse(
        status=status,
        kind=resource.kind,
        name=resource.name,
        source="manual",
        resource_id=resource_id,
        tmdb_id=request.tmdb_id,
        media_type=request.media_type,
        warnings=warnings or [],
    )


def _response(resource_id, kind, name, status):
    return ManualImportResponse(
        status=status,
        resource_id=resource_id,
        kind=kind,
        name=name,
    )
