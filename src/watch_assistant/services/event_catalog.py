"""The versioned, user-facing business event catalog."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Any

from watch_assistant.schemas import LogCategory


@dataclass(frozen=True)
class EventDefinition:
    code: str
    category: LogCategory
    title_zh: str
    message_template_zh: str
    suggestion_zh: str | None = None
    allowed_fields: frozenset[str] = frozenset()
    version: int = 1

    def render(self, fields: dict[str, Any]) -> str:
        try:
            return self.message_template_zh.format_map(_FormatMap(fields))
        except (KeyError, ValueError):
            return f"{self.title_zh}，部分详情暂不可用"


class _FormatMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "未提供"


_COMMON = frozenset(
    {
        "status",
        "count",
        "total",
        "duration_ms",
        "page",
        "media_type",
        "season",
        "hidden_count",
        "hidden_suspicious",
        "hidden_low_quality",
        "hidden_keyword",
        "error_code",
        "changed_fields",
        "cached",
    }
)


def _event(
    code: str,
    category: LogCategory,
    title: str,
    message: str,
    *,
    suggestion: str | None = None,
    fields: frozenset[str] = frozenset(),
) -> EventDefinition:
    # Validate templates when the catalog is imported, rather than at runtime.
    names = {name for _, name, _, _ in Formatter().parse(message) if name}
    if not names <= _COMMON | fields:
        raise ValueError(f"unknown event template fields for {code}")
    return EventDefinition(
        code,
        category,
        title,
        message,
        suggestion,
        _COMMON | fields,
    )


EVENT_CATALOG: dict[str, EventDefinition] = {
    "application.startup": _event(
        "application.startup", LogCategory.SYSTEM, "服务启动", "Watch Assistant 已启动，当前状态：{status}"
    ),
    "application.readiness": _event(
        "application.readiness", LogCategory.SYSTEM, "服务就绪状态变化", "服务就绪状态为“{status}”"
    ),
    "warmup.started": _event(
        "warmup.started", LogCategory.CACHE, "缓存预热开始", "缓存预热任务已开始，处理数量：{total}"
    ),
    "warmup.completed": _event(
        "warmup.completed", LogCategory.CACHE, "缓存预热完成", "缓存预热已完成，成功数量：{count}，失败数量：{hidden_count}"
    ),
    "warmup.failed": _event(
        "warmup.failed", LogCategory.CACHE, "缓存预热失败", "缓存预热未完成，失败数量：{hidden_count}"
    ),
    "search.started": _event(
        "search.started", LogCategory.SEARCH, "资源搜索开始", "已开始{media_type}资源搜索"
    ),
    "search.completed": _event(
        "search.completed", LogCategory.SEARCH, "资源搜索完成", "已完成{media_type}资源搜索，共返回 {count} 条结果，耗时 {duration_ms} 毫秒"
    ),
    "search.failed": _event(
        "search.failed", LogCategory.SEARCH, "资源搜索失败", "{media_type}资源搜索失败，错误码：{error_code}", suggestion="请稍后重试并检查搜索来源状态"
    ),
    "search.cache_hit": _event(
        "search.cache_hit", LogCategory.CACHE, "命中搜索缓存", "已使用缓存中的{media_type}资源结果，共 {count} 条"
    ),
    "resources.page_served": _event(
        "resources.page_served", LogCategory.SEARCH, "资源分页返回", "已返回第 {page} 页资源，共 {count} 条，隐藏 {hidden_count} 条"
    ),
    "inspection.batch_started": _event(
        "inspection.batch_started", LogCategory.INSPECTION, "内容检测开始", "内容检测批次已开始，共提交 {total} 条资源"
    ),
    "inspection.batch_completed": _event(
        "inspection.batch_completed", LogCategory.INSPECTION, "内容检测完成", "内容检测批次已完成，成功数量：{count}，耗时 {duration_ms} 毫秒"
    ),
    "inspection.batch_failed": _event(
        "inspection.batch_failed", LogCategory.INSPECTION, "内容检测失败", "内容检测批次失败，失败数量：{hidden_count}，错误码：{error_code}", suggestion="请稍后重试内容检测"
    ),
    "p115.readiness": _event(
        "p115.readiness", LogCategory.P115, "115 就绪状态变化", "115 服务状态为“{status}”", suggestion="请在设置中重新验证 115 Cookie"
    ),
    "p115.credentials_expired": _event(
        "p115.credentials_expired", LogCategory.P115, "115 登录状态已失效", "当前 115 Cookie 无法继续使用", suggestion="请前往设置重新验证 115 Cookie"
    ),
    "task.submitted": _event(
        "task.submitted", LogCategory.TASK, "推送任务已提交", "推送任务已提交，当前状态：{status}"
    ),
    "task.accepted": _event(
        "task.accepted", LogCategory.TASK, "推送任务已接受", "推送任务已被远端接受"
    ),
    "task.failed": _event(
        "task.failed", LogCategory.TASK, "推送任务失败", "推送任务失败，错误码：{error_code}", suggestion="请检查 115 登录状态和资源有效性"
    ),
    "task.uncertain": _event(
        "task.uncertain", LogCategory.TASK, "推送结果待确认", "请求已超时，暂时无法确认远端是否接受任务", suggestion="请先查询任务状态，不要重复提交"
    ),
    "settings.changed": _event(
        "settings.changed", LogCategory.SETTINGS, "设置已修改", "已修改设置分组“{status}”，变更字段：{changed_fields}"
    ),
    "auth.login.succeeded": _event(
        "auth.login.succeeded", LogCategory.SECURITY, "登录成功", "用户登录成功"
    ),
    "auth.login.failed": _event(
        "auth.login.failed", LogCategory.SECURITY, "登录失败", "用户登录失败", suggestion="请检查密码后重试"
    ),
    "security.csrf_rejected": _event(
        "security.csrf_rejected", LogCategory.SECURITY, "请求安全校验失败", "请求未通过 CSRF 安全校验"
    ),
    "security.permission_denied": _event(
        "security.permission_denied", LogCategory.SECURITY, "权限不足", "当前身份没有执行该操作的权限"
    ),
    "tmdb.request_failed": _event(
        "tmdb.request_failed", LogCategory.SEARCH, "TMDB 请求失败", "TMDB 查询失败，错误码：{error_code}", suggestion="请检查 TMDB 配置或稍后重试"
    ),
    "pansou.partial_failure": _event(
        "pansou.partial_failure", LogCategory.SEARCH, "部分搜索来源失败", "部分搜索来源不可用，已返回可用结果，共 {count} 条"
    ),
    "organize.needs_review": _event(
        "organize.needs_review", LogCategory.ORGANIZE, "资源需要人工确认", "找到多个相近条目，请确认后继续整理"
    ),
    "organize.operation.queued": _event(
        "organize.operation.queued", LogCategory.ORGANIZE, "整理操作已排队", "整理操作已排队，结果：{status}"
    ),
    "organize.operation.updated": _event(
        "organize.operation.updated", LogCategory.ORGANIZE, "整理操作状态已更新", "整理操作状态已更新，结果：{status}"
    ),
    "organize.operation.uncertain": _event(
        "organize.operation.uncertain", LogCategory.ORGANIZE, "整理结果待确认", "整理操作已标记为结果不确定，结果：{status}", suggestion="请先核对远端目录，不要直接重复提交"
    ),
    "organize.operation.completed": _event(
        "organize.operation.completed", LogCategory.ORGANIZE, "整理操作已完成", "整理操作已完成，结果：{status}"
    ),
    "organize.operation.cancelled": _event(
        "organize.operation.cancelled", LogCategory.ORGANIZE, "整理操作已取消", "整理操作已取消，结果：{status}"
    ),
    "organize.operation.retried": _event(
        "organize.operation.retried", LogCategory.ORGANIZE, "整理操作已重试", "整理操作已重新排队，结果：{status}"
    ),
    "strm.cleanup_blocked": _event(
        "strm.cleanup_blocked", LogCategory.STRM, "已阻止 STRM 清理", "本次云端扫描不完整，为避免误删已跳过清理阶段"
    ),
    "library.identity.bound": _event(
        "library.identity.bound",
        LogCategory.LIBRARY,
        "库存媒体身份已绑定",
        "已保存{media_type}库存身份确认，当前状态：{status}",
    ),
    "library.configuration.changed": _event(
        "library.configuration.changed",
        LogCategory.LIBRARY,
        "媒体库配置已更新",
        "媒体库配置已更新，当前状态：{status}，范围校验：{scope_verified}",
        fields=frozenset({"scope_verified"}),
    ),
    "library.scope.verified": _event(
        "library.scope.verified",
        LogCategory.LIBRARY,
        "媒体库范围已校验",
        "媒体库范围校验已完成，当前状态：{status}，媒体库已启用：{enabled}",
        fields=frozenset({"enabled"}),
    ),
    "agent.permission_denied": _event(
        "agent.permission_denied", LogCategory.AGENT, "Agent 权限不足", "当前 Agent 缺少执行所需权限，未执行操作"
    ),
    "agent.request.authenticated": _event(
        "agent.request.authenticated", LogCategory.AGENT, "Agent 请求已认证", "Agent 请求认证成功"
    ),
    "mcp.call": _event(
        "mcp.call", LogCategory.AGENT, "MCP 调用完成", "MCP 方法“{method}”已处理，状态：{status}", fields=frozenset({"method"})
    ),
    "agent.token.created": _event(
        "agent.token.created", LogCategory.SECURITY, "Agent Token 已创建", "已创建 Agent Token，当前状态：{status}"
    ),
    "agent.token.paused": _event(
        "agent.token.paused", LogCategory.SECURITY, "Agent Token 已暂停", "Agent Token 已暂停使用"
    ),
    "agent.token.resumed": _event(
        "agent.token.resumed", LogCategory.SECURITY, "Agent Token 已恢复", "Agent Token 已恢复使用"
    ),
    "agent.token.revoked": _event(
        "agent.token.revoked", LogCategory.SECURITY, "Agent Token 已撤销", "Agent Token 已撤销，不能继续使用"
    ),
    "subscription.created": _event(
        "subscription.created", LogCategory.SUBSCRIPTION, "订阅已创建", "已创建{media_type}订阅，当前状态：{status}"
    ),
    "subscription.changed": _event(
        "subscription.changed", LogCategory.SUBSCRIPTION, "订阅状态已更新", "{media_type}订阅状态已更新为“{status}”"
    ),
    "subscription.checked": _event(
        "subscription.checked", LogCategory.SUBSCRIPTION, "订阅检查完成", "已完成{media_type}订阅检查，匹配资源 {count} 条"
    ),
    "subscription.resources_observed": _event(
        "subscription.resources_observed",
        LogCategory.SUBSCRIPTION,
        "订阅资源观察完成",
        "订阅本次发现 {count} 条新资源，已去重 {hidden_count} 条",
    ),
    "subscription.scheduler_started": _event(
        "subscription.scheduler_started", LogCategory.SUBSCRIPTION, "订阅调度开始", "订阅调度已开始，本次到期检查 {total} 项"
    ),
    "subscription.scheduler_completed": _event(
        "subscription.scheduler_completed", LogCategory.SUBSCRIPTION, "订阅调度完成", "订阅调度已完成，成功 {count} 项，失败 {hidden_count} 项"
    ),
    "quality_profile.created": _event(
        "quality_profile.created", LogCategory.QUALITY, "质量策略已创建", "已创建质量策略，当前状态：{status}"
    ),
    "quality_profile.changed": _event(
        "quality_profile.changed", LogCategory.QUALITY, "质量策略已更新", "质量策略已更新，当前状态：{status}"
    ),
    "workflow.created": _event(
        "workflow.created", LogCategory.TASK, "工作流已创建", "已创建端到端工作流，当前状态：{status}"
    ),
    "workflow.stage_changed": _event(
        "workflow.stage_changed", LogCategory.TASK, "工作流阶段已更新", "工作流阶段“{stage}”已更新，当前状态：{status}", fields=frozenset({"stage"})
    ),
    "workflow.approval_decided": _event(
        "workflow.approval_decided",
        LogCategory.TASK,
        "工作流人工确认已处理",
        "工作流人工确认结果：{status}",
    ),
    "workflow.cancelled": _event(
        "workflow.cancelled",
        LogCategory.TASK,
        "工作流已取消",
        "工作流已取消，当前状态：{status}",
    ),
    "notification.created": _event(
        "notification.created", LogCategory.TASK, "站内通知已创建", "已创建站内通知，当前严重度：{status}"
    ),
    "notification.aggregated": _event(
        "notification.aggregated", LogCategory.TASK, "站内通知已合并", "相同通知已合并，累计 {count} 次"
    ),
    "notification.read": _event(
        "notification.read", LogCategory.TASK, "站内通知已读", "站内通知已标记为已读"
    ),
    "notification.read_all": _event(
        "notification.read_all", LogCategory.TASK, "通知已全部读完", "已将 {count} 条站内通知标记为已读"
    ),
    "notification.preferences_changed": _event(
        "notification.preferences_changed", LogCategory.TASK, "通知偏好已更新", "站内通知当前状态：{status}"
    ),
    "backup.created": _event(
        "backup.created", LogCategory.SYSTEM, "备份已创建", "数据库备份已创建，状态：{status}，数量：{count}"
    ),
    "backup.failed": _event(
        "backup.failed", LogCategory.SYSTEM, "备份失败", "数据库备份失败，错误码：{error_code}", suggestion="请检查磁盘空间和备份目录权限"
    ),
    "backup.restore_preview": _event(
        "backup.restore_preview", LogCategory.SYSTEM, "恢复预览已完成", "备份恢复预览状态：{status}", suggestion="恢复覆盖前请确认服务已排空任务并保留当前快照"
    ),
    "media.detail.performance": _event(
        "media.detail.performance",
        LogCategory.SEARCH,
        "详情性能指标",
        "{media_type}详情{stage}阶段状态为“{status}”，耗时 {duration_ms} 毫秒，缓存：{cached}",
        fields=frozenset({"stage"}),
    ),
    "media.detail.race_dropped": _event(
        "media.detail.race_dropped",
        LogCategory.SEARCH,
        "详情迟到响应已丢弃",
        "{media_type}详情{stage}阶段的迟到响应已丢弃",
        fields=frozenset({"stage"}),
    ),
    "media.detail.request_cancelled": _event(
        "media.detail.request_cancelled",
        LogCategory.SEARCH,
        "详情请求已取消",
        "{media_type}详情{stage}阶段请求已取消",
        fields=frozenset({"stage"}),
    ),
    "observability.unknown_event": _event(
        "observability.unknown_event", LogCategory.SYSTEM, "未登记事件", "系统尝试记录未登记事件：{status}", suggestion="请为新业务事件补充事件目录定义", fields=frozenset({"event_code"})
    ),
}


def get_event_definition(event: str) -> EventDefinition | None:
    return EVENT_CATALOG.get(event)
