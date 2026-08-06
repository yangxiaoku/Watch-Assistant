"""Read-only deployment and compatibility diagnostics."""

from __future__ import annotations

import importlib.metadata
import platform
import sqlite3
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from watch_assistant.migrations import MIGRATIONS
from watch_assistant.schemas import (
    CompatibilityStatus,
    DeploymentComponentResponse,
    DeploymentDiagnosticsResponse,
)

_EXPECTED_P115CLIENT = "0.0.9.6.5.1"


class DeploymentDiagnosticsService:
    """Build a conservative, non-sensitive view of runtime compatibility."""

    def __init__(self, engine: AsyncEngine, state: Any) -> None:
        self._engine = engine
        self._state = state

    async def snapshot(self) -> DeploymentDiagnosticsResponse:
        applied, integrity = await self._database_snapshot()
        expected = [migration.id for migration in MIGRATIONS]
        applied_set = set(applied)
        pending = [migration_id for migration_id in expected if migration_id not in applied_set]
        components = [
            self._application_component(),
            self._python_component(),
            self._sqlite_component(),
            self._p115client_component(),
            self._database_component(applied, pending, integrity),
            await self._credential_component(),
            self._pansou_component(),
            self._qbittorrent_component(),
            self._p115_component(),
        ]
        if any(item.status == CompatibilityStatus.UNSUPPORTED for item in components):
            overall = CompatibilityStatus.UNSUPPORTED
        elif any(item.status == CompatibilityStatus.UNKNOWN for item in components):
            overall = CompatibilityStatus.UNKNOWN
        elif any(item.status == CompatibilityStatus.DEGRADED for item in components):
            overall = CompatibilityStatus.DEGRADED
        else:
            overall = CompatibilityStatus.SUPPORTED
        return DeploymentDiagnosticsResponse(
            release=getattr(self._state, "release", "unknown"),
            generated_at=datetime.now(UTC),
            overall_status=overall,
            write_safe=overall == CompatibilityStatus.SUPPORTED,
            components=components,
            applied_migrations=applied,
            pending_migrations=pending,
            database_integrity=integrity,
        )

    async def _database_snapshot(self) -> tuple[list[str], CompatibilityStatus]:
        try:
            async with self._engine.connect() as connection:
                applied = list(
                    await connection.scalars(
                        text(
                            "SELECT migration_id FROM schema_migrations "
                            "ORDER BY migration_id"
                        )
                    )
                )
                if connection.dialect.name != "sqlite":
                    return applied, CompatibilityStatus.UNKNOWN
                result = await connection.scalar(text("PRAGMA integrity_check"))
                return applied, (
                    CompatibilityStatus.SUPPORTED
                    if result == "ok"
                    else CompatibilityStatus.UNSUPPORTED
                )
        except Exception:  # noqa: BLE001 - diagnostics must not leak storage details
            return [], CompatibilityStatus.UNKNOWN

    def _application_component(self) -> DeploymentComponentResponse:
        release = getattr(self._state, "release", "unknown")
        supported = release != "unknown"
        return DeploymentComponentResponse(
            key="watch_assistant",
            name_zh="Watch Assistant 后端",
            status=(
                CompatibilityStatus.SUPPORTED
                if supported
                else CompatibilityStatus.UNKNOWN
            ),
            version=release,
            expected="已构建版本或可信发布摘要",
            message_zh="应用版本已识别" if supported else "应用版本无法确认",
            suggestion_zh=(
                None
                if supported
                else "配置可信发布版本后再执行升级或写操作"
            ),
        )

    @staticmethod
    def _python_component() -> DeploymentComponentResponse:
        version = platform.python_version()
        supported = _version_tuple(version) >= (3, 12)
        return DeploymentComponentResponse(
            key="python",
            name_zh="Python 运行时",
            status=(
                CompatibilityStatus.SUPPORTED
                if supported
                else CompatibilityStatus.UNSUPPORTED
            ),
            version=version,
            expected=">=3.12",
            message_zh="Python 版本符合要求" if supported else "Python 版本过低",
            suggestion_zh=None if supported else "升级到 Python 3.12 或更高版本",
        )

    @staticmethod
    def _sqlite_component() -> DeploymentComponentResponse:
        version = sqlite3.sqlite_version
        supported = _version_tuple(version) >= (3, 35, 0)
        return DeploymentComponentResponse(
            key="sqlite",
            name_zh="SQLite",
            status=(
                CompatibilityStatus.SUPPORTED
                if supported
                else CompatibilityStatus.UNSUPPORTED
            ),
            version=version,
            expected=">=3.35（在线备份）",
            capabilities={"online_backup": supported},
            message_zh=(
                "SQLite 支持在线备份"
                if supported
                else "SQLite 版本不支持要求的在线备份能力"
            ),
            suggestion_zh=None if supported else "升级 SQLite 后再执行备份或升级",
        )

    @staticmethod
    def _p115client_component() -> DeploymentComponentResponse:
        try:
            version = importlib.metadata.version("p115client")
        except importlib.metadata.PackageNotFoundError:
            version = None
        status = (
            CompatibilityStatus.UNKNOWN
            if version is None
            else CompatibilityStatus.SUPPORTED
            if version == _EXPECTED_P115CLIENT
            else CompatibilityStatus.DEGRADED
        )
        return DeploymentComponentResponse(
            key="p115client",
            name_zh="p115client",
            status=status,
            version=version,
            expected=f"=={_EXPECTED_P115CLIENT}",
            message_zh=(
                "p115client 版本符合项目锁定版本"
                if status == CompatibilityStatus.SUPPORTED
                else "p115client 版本需要人工确认"
            ),
            suggestion_zh=(
                None
                if status == CompatibilityStatus.SUPPORTED
                else "确认 p115client 与当前适配器契约兼容，未知时保持只读"
            ),
        )

    @staticmethod
    def _database_component(
        applied: list[str], pending: list[str], integrity: CompatibilityStatus
    ) -> DeploymentComponentResponse:
        status = (
            CompatibilityStatus.UNSUPPORTED
            if integrity == CompatibilityStatus.UNSUPPORTED
            else CompatibilityStatus.DEGRADED
            if pending
            else integrity
        )
        return DeploymentComponentResponse(
            key="database",
            name_zh="数据库 Schema",
            status=status,
            version=applied[-1] if applied else None,
            expected=f"{len(MIGRATIONS)} 个有序迁移全部已应用",
            capabilities={"integrity_check": integrity == CompatibilityStatus.SUPPORTED},
            message_zh=(
                "数据库完整且 Schema 已是最新"
                if status == CompatibilityStatus.SUPPORTED
                else "数据库存在待执行迁移"
                if pending
                else "数据库完整性无法确认或检查失败"
            ),
            suggestion_zh=(
                f"先完成迁移：{', '.join(pending)}"
                if pending
                else None
                if status == CompatibilityStatus.SUPPORTED
                else "检查数据库文件和迁移记录；未知时不要执行写入升级"
            ),
        )

    async def _credential_component(self) -> DeploymentComponentResponse:
        service = getattr(self._state, "credential_service", None)
        if service is None:
            return DeploymentComponentResponse(
                key="tmdb",
                name_zh="TMDB 凭据",
                status=CompatibilityStatus.UNKNOWN,
                message_zh="凭据服务不可用",
                suggestion_zh="确认应用配置和加密密钥可读取",
            )
        try:
            snapshot = await service.snapshot()
            configured = bool(snapshot.get("tmdb", {}).get("configured"))
        except Exception:  # noqa: BLE001 - credential details stay opaque
            configured = False
        return DeploymentComponentResponse(
            key="tmdb",
            name_zh="TMDB 凭据",
            status=(
                CompatibilityStatus.SUPPORTED
                if configured
                else CompatibilityStatus.DEGRADED
            ),
            capabilities={"configured": configured},
            message_zh="TMDB 凭据已配置" if configured else "TMDB 凭据未配置",
            suggestion_zh=None if configured else "配置 TMDB 凭据后再启用媒体匹配",
        )

    def _pansou_component(self) -> DeploymentComponentResponse:
        configured = bool(getattr(self._state, "search_service", None))
        return DeploymentComponentResponse(
            key="pansou",
            name_zh="PanSou 搜索服务",
            status=(
                CompatibilityStatus.UNKNOWN
                if configured
                else CompatibilityStatus.DEGRADED
            ),
            message_zh=(
                "PanSou 已配置但连通性未检查"
                if configured
                else "PanSou 服务未配置"
            ),
            suggestion_zh=(
                "通过受控搜索检查验证 PanSou 契约"
                if configured
                else "配置 PanSou 地址后再启用搜索"
            ),
        )

    def _qbittorrent_component(self) -> DeploymentComponentResponse:
        configured = bool(getattr(self._state, "inspection_supported", False))
        return DeploymentComponentResponse(
            key="qbittorrent",
            name_zh="qBittorrent 检测服务",
            status=(
                CompatibilityStatus.UNKNOWN
                if configured
                else CompatibilityStatus.DEGRADED
            ),
            capabilities={"inspection": configured},
            message_zh=(
                "qBittorrent 已接入但版本未验证"
                if configured
                else "qBittorrent 检测未启用"
            ),
            suggestion_zh=(
                "验证 qBittorrent 版本和认证后再启用检测写流程"
                if configured
                else "按需配置 qBittorrent 检测服务"
            ),
        )

    def _p115_component(self) -> DeploymentComponentResponse:
        service = getattr(self._state, "p115_settings_service", None)
        if service is None:
            return DeploymentComponentResponse(
                key="p115",
                name_zh="115 服务",
                status=CompatibilityStatus.UNKNOWN,
                message_zh="115 状态不可确认",
                suggestion_zh="确认只读凭据和目标目录配置",
            )
        try:
            snapshot = service.snapshot(
                runtime_ready=getattr(self._state, "p115_ready", False) is True,
                runtime_magnet_capability=bool(
                    getattr(self._state, "push_capabilities", {}).get("magnet", False)
                ),
                runtime_share_capability=bool(
                    getattr(self._state, "push_capabilities", {}).get("share", False)
                ),
            )
            ready = bool(snapshot.ready)
            enabled = bool(snapshot.enabled)
        except Exception:  # noqa: BLE001 - external credential details stay opaque
            ready = False
            enabled = True
        status = (
            CompatibilityStatus.SUPPORTED
            if ready
            else CompatibilityStatus.DEGRADED
            if not enabled
            else CompatibilityStatus.UNKNOWN
        )
        return DeploymentComponentResponse(
            key="p115",
            name_zh="115 服务",
            status=status,
            capabilities={"read_only": ready, "magnet_push": ready},
            message_zh="115 只读和磁力能力已就绪" if ready else "115 能力未就绪或尚未验证",
            suggestion_zh=None if ready else "先完成只读凭据、目标目录和最小测试目录验证",
        )


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)

