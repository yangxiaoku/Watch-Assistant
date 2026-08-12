"""Durable subscription identities and safe manual checks."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import (
    Resource,
    Subscription,
    SubscriptionResourceObservation,
)
from watch_assistant.schemas import (
    SubscriptionCheckResponse,
    SubscriptionCreateRequest,
    SubscriptionMutationRequest,
    SubscriptionResourceObservationResponse,
    SubscriptionResponse,
    SubscriptionStatus,
)
from watch_assistant.services.observability import EventLogger, emit_event


class SubscriptionNotFound(LookupError):
    pass


class SubscriptionConflict(ValueError):
    pass


# 搜索期间不得被旧任务改写的状态:取消/暂停/完成都是用户的最终意图。
_TERMINAL_SUBSCRIPTION_STATES = frozenset(
    {
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.COMPLETED,
    }
)


class SubscriptionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        search_service,
        *,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._search = search_service
        self._event_logger = event_logger
        # 并发创建同一订阅时,先查后插的窗口需要进程内互斥:SQLite 的
        # 部分唯一索引(迁移 070)是跨进程兜底,但同一事件循环里两个任务
        # 同时通过 _find_scope 再各自 commit 时,写串行化下的交错可能
        # 让两个插入都“成功”。加锁把创建串行化,让第二个创建必然命中
        # 已存在检查(与 M2 回归测试的确定性一致)。
        self._create_lock = asyncio.Lock()

    async def create(self, request: SubscriptionCreateRequest) -> SubscriptionResponse:
        async with self._create_lock:
            return await self._create_locked(request)

    async def _create_locked(
        self, request: SubscriptionCreateRequest
    ) -> SubscriptionResponse:
        async with self._session_factory() as session:
            existing = await self._find_scope(session, request)
            if existing is not None and existing.status != SubscriptionStatus.CANCELLED:
                raise SubscriptionConflict("subscription_exists")
            now = datetime.now(UTC)
            item = Subscription(
                id="sub_" + uuid4().hex,
                tmdb_id=request.tmdb_id,
                media_type=request.media_type,
                season_number=request.season_number,
                episode_start=request.episode_start,
                episode_end=request.episode_end,
                mode=request.mode,
                quality_profile_id=request.quality_profile_id,
                status=SubscriptionStatus.ACTIVE,
                next_check_at=now,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise SubscriptionConflict("subscription_exists") from None
        await emit_event(
            self._event_logger,
            "subscription.created",
            fields={"status": item.status.value, "media_type": item.media_type.value},
            resource_type="tmdb",
            resource_id=f"{item.media_type.value}:{item.tmdb_id}",
        )
        return _response(item)

    async def list(self) -> list[SubscriptionResponse]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Subscription).order_by(Subscription.updated_at.desc())
            )
            return [_response(row) for row in rows]

    async def get(self, subscription_id: str) -> SubscriptionResponse:
        async with self._session_factory() as session:
            item = await session.get(Subscription, subscription_id)
            if item is None:
                raise SubscriptionNotFound(subscription_id)
            return _response(item)

    async def mutate(
        self,
        subscription_id: str,
        request: SubscriptionMutationRequest,
        action: str,
    ) -> SubscriptionResponse:
        async with self._session_factory() as session:
            item = await session.get(Subscription, subscription_id)
            if item is None:
                raise SubscriptionNotFound(subscription_id)
            if item.revision != request.revision:
                raise SubscriptionConflict("subscription_conflict")
            if action == "pause":
                if item.status in {SubscriptionStatus.CANCELLED, SubscriptionStatus.COMPLETED}:
                    raise SubscriptionConflict("subscription_not_pauseable")
                item.status = SubscriptionStatus.PAUSED
            elif action == "resume":
                if item.status in {
                    SubscriptionStatus.CANCELLED,
                    SubscriptionStatus.COMPLETED,
                }:
                    # COMPLETED 与 CANCELLED 一样是终态,不得复活(与 check()
                    # 的非活跃判定一致)。复用已注册的 subscription_cancelled 码。
                    raise SubscriptionConflict("subscription_cancelled")
                item.status = SubscriptionStatus.ACTIVE
                item.next_check_at = datetime.now(UTC)
            elif action == "cancel":
                item.status = SubscriptionStatus.CANCELLED
                item.next_check_at = None
            else:
                raise ValueError("unknown_subscription_action")
            item.revision += 1
            item.updated_at = datetime.now(UTC)
            await session.commit()
        await emit_event(
            self._event_logger,
            "subscription.changed",
            fields={"status": item.status.value, "media_type": item.media_type.value},
            resource_type="tmdb",
            resource_id=f"{item.media_type.value}:{item.tmdb_id}",
        )
        return _response(item)

    async def check(self, subscription_id: str) -> SubscriptionCheckResponse:
        async with self._session_factory() as session:
            item = await session.get(Subscription, subscription_id)
            if item is None:
                raise SubscriptionNotFound(subscription_id)
            if item.status in {
                SubscriptionStatus.PAUSED,
                SubscriptionStatus.CANCELLED,
                SubscriptionStatus.COMPLETED,
            }:
                raise SubscriptionConflict("subscription_not_active")
            tmdb_id = item.tmdb_id
            media_type = item.media_type
            season_number = item.season_number
        try:
            result = await self._search.search(
                tmdb_id,
                media_type=media_type,
                refresh=True,
                season_number=season_number,
            )
        except Exception:  # noqa: BLE001 - persist a stable subscription error
            async with self._session_factory() as session:
                item = await session.get(Subscription, subscription_id)
                if item is None:
                    raise SubscriptionNotFound(subscription_id)
                if item.status in _TERMINAL_SUBSCRIPTION_STATES:
                    # 搜索期间被取消/暂停/完成:保留用户意图,不写错误状态也不重新排程
                    raise SubscriptionConflict("subscription_not_active") from None
                item.last_checked_at = datetime.now(UTC)
                item.last_error_code = "search_unavailable"
                item.next_check_at = datetime.now(UTC) + timedelta(hours=6)
                item.revision += 1
                await session.commit()
            await emit_event(
                self._event_logger,
                "subscription.check_failed",
                fields={
                    "status": "failed",
                    "error_code": "search_unavailable",
                    "media_type": media_type.value,
                },
                resource_type="subscription",
                resource_id=subscription_id,
            )
            raise SubscriptionConflict("subscription_check_failed") from None

        resource_ids = list(
            dict.fromkeys(resource.resource_id for resource in result.results)
        )
        new_resource_ids = await self._record_observations(subscription_id, resource_ids)
        async with self._session_factory() as session:
            item = await session.get(Subscription, subscription_id)
            if item is None:
                raise SubscriptionNotFound(subscription_id)
            if item.status in _TERMINAL_SUBSCRIPTION_STATES:
                # 搜索期间被取消/暂停/完成:不允许旧任务把订阅改回 MATCHED 复活
                raise SubscriptionConflict("subscription_not_active") from None
            # 用条件 UPDATE 覆盖写回:在“重读终态 → commit”的窗口内若被
            # pause/resume/cancel 并发修改,本事务的过期快照不得覆盖用户意图。
            # WHERE 同时要求 status 仍活跃且 revision 未变(乐观锁);rowcount
            # 非 1 即表示订阅已离开活跃状态或被并发修改,直接抛
            # subscription_not_active 保持终态。
            now = datetime.now(UTC)
            status = (
                SubscriptionStatus.MATCHED
                if resource_ids
                else SubscriptionStatus.NO_MATCH
            )
            result = await session.execute(
                update(Subscription)
                .where(
                    Subscription.id == subscription_id,
                    Subscription.status.not_in(
                        [state.value for state in _TERMINAL_SUBSCRIPTION_STATES]
                    ),
                    Subscription.revision == item.revision,
                )
                .values(
                    last_checked_at=now,
                    last_match_count=len(resource_ids),
                    last_error_code=None,
                    status=status,
                    next_check_at=now + timedelta(hours=6),
                    revision=item.revision + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise SubscriptionConflict("subscription_not_active") from None
            await session.commit()
            # ORM-enabled UPDATE 会把新值同步进 identity map,但仍以 DB 为准
            # 刷新一次,避免响应里的 revision 与已提交数据脱节。
            await session.refresh(item)
            response = _response(item)
        await emit_event(
            self._event_logger,
            "subscription.checked",
            fields={
                "status": response.status.value,
                "count": len(resource_ids),
                "media_type": response.media_type.value,
            },
            resource_type="tmdb",
            resource_id=f"{response.media_type.value}:{response.tmdb_id}",
        )
        await emit_event(
            self._event_logger,
            "subscription.resources_observed",
            fields={
                "status": "new" if new_resource_ids else "deduplicated",
                "count": len(new_resource_ids),
                "hidden_count": len(resource_ids) - len(new_resource_ids),
                "media_type": response.media_type.value,
            },
            resource_type="subscription",
            resource_id=subscription_id,
        )
        return SubscriptionCheckResponse(
            subscription=response,
            matched_count=len(resource_ids),
            resource_ids=resource_ids,
            new_resource_ids=new_resource_ids,
        )

    async def list_observations(
        self, subscription_id: str, *, limit: int = 50
    ) -> list[SubscriptionResourceObservationResponse]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._session_factory() as session:
            if await session.get(Subscription, subscription_id) is None:
                raise SubscriptionNotFound(subscription_id)
            rows = list(
                (
                    await session.scalars(
                        select(SubscriptionResourceObservation)
                        .where(
                            SubscriptionResourceObservation.subscription_id
                            == subscription_id
                        )
                        .order_by(
                            SubscriptionResourceObservation.last_seen_at.desc(),
                            SubscriptionResourceObservation.id,
                        )
                        .limit(limit)
                    )
                ).all()
            )
        return [
            SubscriptionResourceObservationResponse(
                resource_id=row.resource_id,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                seen_count=row.seen_count,
            )
            for row in rows
        ]

    async def _record_observations(
        self, subscription_id: str, resource_ids: list[str]
    ) -> list[str]:
        if not resource_ids:
            return []
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            resources = {
                row.id: row
                for row in (
                    await session.scalars(
                        select(Resource).where(Resource.id.in_(resource_ids))
                    )
                ).all()
            }
            canonical_by_resource = {
                resource_id: (
                    resources[resource_id].canonical_key
                    if resource_id in resources
                    else f"resource:{resource_id}"
                )
                for resource_id in resource_ids
            }
            existing = {
                row.canonical_key: row
                for row in (
                    await session.scalars(
                        select(SubscriptionResourceObservation).where(
                            SubscriptionResourceObservation.subscription_id
                            == subscription_id,
                            SubscriptionResourceObservation.canonical_key.in_(
                                list(canonical_by_resource.values())
                            ),
                        )
                    )
                ).all()
            }
            new_resource_ids: list[str] = []
            for resource_id in resource_ids:
                canonical_key = canonical_by_resource[resource_id]
                row = existing.get(canonical_key)
                if row is None:
                    row = SubscriptionResourceObservation(
                        id=hashlib.sha256(
                            f"{subscription_id}:{canonical_key}".encode()
                        ).hexdigest(),
                        subscription_id=subscription_id,
                        resource_id=resource_id,
                        canonical_key=canonical_key,
                        first_seen_at=now,
                        last_seen_at=now,
                        seen_count=1,
                    )
                    session.add(row)
                    existing[canonical_key] = row
                    new_resource_ids.append(resource_id)
                else:
                    row.resource_id = resource_id
                    row.last_seen_at = now
                    row.seen_count += 1
            await session.commit()
        return new_resource_ids

    async def _find_scope(
        self, session: AsyncSession, request: SubscriptionCreateRequest
    ) -> Subscription | None:
        rows = await session.scalars(
            select(Subscription).where(
                Subscription.tmdb_id == request.tmdb_id,
                Subscription.media_type == request.media_type,
                Subscription.season_number == request.season_number,
                Subscription.episode_start == request.episode_start,
                Subscription.episode_end == request.episode_end,
            )
        )
        return next(iter(rows), None)


def _response(item: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse.model_validate(item, from_attributes=True)
