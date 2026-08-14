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

    def render(
        self,
        fields: dict[str, Any],
        counts: dict[str, Any] | None = None,
    ) -> str:
        try:
            render_fields = dict(fields)
            render_fields.update(counts or {})
            return self.message_template_zh.format_map(_FormatMap(render_fields))
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
    "search.source_degraded": _event(
        "search.source_degraded",
        LogCategory.SEARCH,
        "搜索来源已降级",
        "已将{source}搜索来源标记为暂不可用",
        suggestion="请检查搜索来源状态后重试",
        fields=frozenset({"source"}),
    ),
    "search.slow_merge_completed": _event(
        "search.slow_merge_completed",
        LogCategory.SEARCH,
        "慢索引器后台合并完成",
        "慢索引器（{source}）后台搜索完成，合并结果 {count} 条，状态：{status}",
        fields=frozenset({"source", "count", "status"}),
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
    "p115.checkin.succeeded": _event(
        "p115.checkin.succeeded",
        LogCategory.BUSINESS,
        "115 签到成功",
        "115 每日签到成功,获得 {points} 积分,连续 {continuous_day} 天",
        fields=frozenset({"points", "continuous_day"}),
    ),
    "p115.checkin.failed": _event(
        "p115.checkin.failed",
        LogCategory.BUSINESS,
        "115 签到失败",
        "115 每日签到失败,错误码:{error_code}",
    ),
    "task.submitted": _event(
        "task.submitted", LogCategory.TASK, "推送任务已提交", "推送任务已提交，等待 115 受理"
    ),
    "task.accepted": _event(
        "task.accepted", LogCategory.TASK, "推送请求已受理", "115 已受理推送请求，等待文件可用证据"
    ),
    "task.downloading": _event(
        "task.downloading", LogCategory.TASK, "文件正在下载", "115 正在下载文件，尚未取得文件可用证据"
    ),
    "task.availability_verified": _event(
        "task.availability_verified",
        LogCategory.TASK,
        "文件已确认可用",
        "已通过只读核对取得文件可用证据",
    ),
    "task.failed": _event(
        "task.failed", LogCategory.TASK, "推送任务失败", "推送任务失败，错误码：{error_code}", suggestion="请检查 115 登录状态和资源有效性"
    ),
    "task.uncertain": _event(
        "task.uncertain", LogCategory.TASK, "推送结果待确认", "请求已超时，暂时无法确认远端是否接受任务", suggestion="请先查询任务状态，不要重复提交"
    ),
    "task.cancelled": _event(
        "task.cancelled", LogCategory.TASK, "推送任务已取消", "尚未提交的推送任务已取消"
    ),
    "task.recovery_failed": _event(
        "task.recovery_failed",
        LogCategory.TASK,
        "任务租约回收失败",
        "周期性租约回收未能完成，卡住的任务会延迟到下次回收",
    ),
    "inventory.refresh.started": _event(
        "inventory.refresh.started",
        LogCategory.LIBRARY,
        "媒体库库存自动刷新开始",
        "推送前正在自动刷新媒体库库存，共 {total} 个范围",
    ),
    "inventory.refresh.completed": _event(
        "inventory.refresh.completed",
        LogCategory.LIBRARY,
        "媒体库库存自动刷新完成",
        "媒体库库存自动刷新完成，已刷新 {count} 个范围",
    ),
    "inventory.refresh.failed": _event(
        "inventory.refresh.failed",
        LogCategory.LIBRARY,
        "媒体库库存自动刷新失败",
        "媒体库库存自动刷新失败，失败范围：{hidden_count}，错误码：{error_code}",
        suggestion="请检查 115 登录状态和媒体库范围配置后重试",
    ),
    "library.scan_schedule.completed": _event(
        "library.scan_schedule.completed",
        LogCategory.LIBRARY,
        "媒体库自动扫描检查完成",
        "媒体库自动扫描检查完成：库 {libraries} 个，新入队 {scanned}，跳过 {skipped}，失败 {failed}",
        fields=frozenset({"libraries", "scanned", "skipped", "failed"}),
    ),
    "library.scan_schedule.failed": _event(
        "library.scan_schedule.failed",
        LogCategory.LIBRARY,
        "媒体库自动扫描入队失败",
        "媒体库自动扫描入队失败，库：{library_id}",
        suggestion="请检查 115 登录状态和媒体库范围配置后重试",
        fields=frozenset({"library_id"}),
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
        "organize.needs_review",
        LogCategory.ORGANIZE,
        "整理预览需要人工确认",
        "整理预览涉及 {count} 个文件，需要人工确认后继续",
        suggestion="请打开整理预览，核对匹配和目标冲突后再确认",
    ),
    "organize.preview.created": _event(
        "organize.preview.created",
        LogCategory.ORGANIZE,
        "整理预览已生成",
        "整理预览已生成，涉及 {count} 个文件",
    ),
    "organize.plan.awaiting_confirmation": _event(
        "organize.plan.awaiting_confirmation",
        LogCategory.ORGANIZE,
        "整理计划等待确认",
        "整理计划已保存，等待人工确认，涉及 {count} 个文件",
    ),
    "organize.plan.confirmed": _event(
        "organize.plan.confirmed",
        LogCategory.ORGANIZE,
        "整理计划已确认",
        "整理计划已确认",
    ),
    "organize.plan.ignored": _event(
        "organize.plan.ignored",
        LogCategory.ORGANIZE,
        "整理计划已忽略",
        "整理计划已忽略",
    ),
    "organize.plan.alias_changed": _event(
        "organize.plan.alias_changed",
        LogCategory.ORGANIZE,
        "整理计划别名已更新",
        "整理计划别名已更新",
    ),
    "organize.directory.provisioned": _event(
        "organize.directory.provisioned",
        LogCategory.ORGANIZE,
        "整理目标目录已创建",
        "确认后的整理操作已创建目标目录，共 {count} 个目录",
    ),
    "organize.automation.blocked": _event(
        "organize.automation.blocked",
        LogCategory.ORGANIZE,
        "自动整理已阻断",
        "自动整理已阻断，来源：{source_directory_id}，原因：{message_zh}（错误码：{error_code}）",
        suggestion="请先核对来源目录扫描状态和整理配置，再重新运行",
        fields=frozenset({"source_directory_id", "message_zh"}),
    ),
    "organize.automation.cleaned": _event(
        "organize.automation.cleaned",
        LogCategory.ORGANIZE,
        "自动清理完成",
        "未识别小文件已删除 {small_files} 个，空目录已清理 {empty_dirs} 个",
        suggestion="删除文件已进入 115 回收站，可在回收站中恢复",
        fields=frozenset({"small_files", "empty_dirs"}),
    ),
    "organize.operation.queued": _event(
        "organize.operation.queued", LogCategory.ORGANIZE, "整理操作已排队", "整理操作已排队"
    ),
    "organize.operation.updated": _event(
        "organize.operation.updated", LogCategory.ORGANIZE, "整理操作状态已更新", "整理操作状态已更新"
    ),
    "organize.operation.failed": _event(
        "organize.operation.failed",
        LogCategory.ORGANIZE,
        "整理操作失败",
        "整理操作失败，错误码：{error_code}",
        suggestion="请打开整理工作台检查计划状态，确认失败原因后再重试",
    ),
    "organize.operation.uncertain": _event(
        "organize.operation.uncertain", LogCategory.ORGANIZE, "整理结果待确认", "整理操作结果待确认，请先核对远端目录", suggestion="请先核对远端目录，不要直接重复提交"
    ),
    "organize.operation.completed": _event(
        "organize.operation.completed", LogCategory.ORGANIZE, "整理操作已完成", "整理操作已完成，结果：{status}"
    ),
    "organize.operation.cancelled": _event(
        "organize.operation.cancelled", LogCategory.ORGANIZE, "整理操作已取消", "整理操作已取消，结果：{status}"
    ),
    "organize.operation.reconciled": _event(
        "organize.operation.reconciled",
        LogCategory.ORGANIZE,
        "整理结果已核对",
        "整理操作已通过远端只读核对，结果：{status}",
        suggestion="请继续关注后续库存和目录同步状态",
    ),
    "organize.operation.reconciled_not_applied": _event(
        "organize.operation.reconciled_not_applied",
        LogCategory.ORGANIZE,
        "整理结果已核对为未执行",
        "整理操作已核对为未执行，结果：{status}",
        suggestion="确认计划仍然有效后，再重新确认并排队",
    ),
    "organize.operation.cancel_requested": _event(
        "organize.operation.cancel_requested",
        LogCategory.ORGANIZE,
        "已请求中止整理操作",
        "已请求中止正在运行的整理操作，结果：{status}",
    ),
    "organize.operation.retried": _event(
        "organize.operation.retried", LogCategory.ORGANIZE, "整理操作已重试", "整理操作已重新排队，结果：{status}"
    ),
    "strm.cleanup_blocked": _event(
        "strm.cleanup_blocked", LogCategory.STRM, "已阻止 STRM 清理", "本次云端扫描不完整，为避免误删已跳过清理阶段"
    ),
    "strm.cleanup.plan.created": _event(
        "strm.cleanup.plan.created",
        LogCategory.STRM,
        "STRM 清理计划已生成",
        "STRM 清理计划已生成，包含 {count} 个候选项",
    ),
    "strm.cleanup.applied": _event(
        "strm.cleanup.applied",
        LogCategory.STRM,
        "STRM 清理计划已执行",
        "STRM 清理计划已执行，处理 {count} 个受管项",
    ),
    "strm.verify.completed": _event(
        "strm.verify.completed",
        LogCategory.STRM,
        "STRM 校验已完成",
        "STRM 校验已完成，结果：{status}，检查 {count} 个条目",
    ),
    "strm.dirty_consumed": _event(
        "strm.dirty_consumed", LogCategory.STRM, "目录变更已完成增量对账", "目录变更已完成增量对账，当前状态：{status}"
    ),
    "strm.dirty_failed": _event(
        "strm.dirty_failed",
        LogCategory.STRM,
        "STRM 增量对账失败",
        "STRM 增量对账失败，错误码：{error_code}",
        suggestion="请检查媒体库扫描和 STRM 设置，确认失败原因后再重试",
    ),
    "strm.dirty_skipped": _event(
        "strm.dirty_skipped", LogCategory.STRM, "已跳过目录增量对账", "整理完成后未启用 STRM 联动"
    ),
    "library.empty_directory_cleanup.review_required": _event(
        "library.empty_directory_cleanup.review_required",
        LogCategory.LIBRARY,
        "受管空目录需要确认",
        "发现受管空目录，已等待人工预览和一次确认，当前状态：{status}",
    ),
    "library.empty_directory_cleanup.plan.created": _event(
        "library.empty_directory_cleanup.plan.created",
        LogCategory.LIBRARY,
        "空目录清理计划已生成",
        "空目录清理计划已生成，包含 {count} 个候选项",
    ),
    "library.empty_directory_cleanup.applied": _event(
        "library.empty_directory_cleanup.applied",
        LogCategory.LIBRARY,
        "空目录清理计划已执行",
        "空目录清理计划已执行，处理 {count} 个受管目录",
    ),
    "library.small_file_cleanup.previewed": _event(
        "library.small_file_cleanup.previewed",
        LogCategory.LIBRARY,
        "小文件清理预览已生成",
        "小文件清理预览已生成，共 {count} 个低于阈值的小文件",
    ),
    "library.small_file_cleanup.applied": _event(
        "library.small_file_cleanup.applied",
        LogCategory.LIBRARY,
        "小文件清理已执行",
        "小文件清理已执行，回收 {count} 个小文件（可恢复）",
    ),
    "library.object.permanently_deleted": _event(
        "library.object.permanently_deleted",
        LogCategory.LIBRARY,
        "文件已永久删除",
        "文件已永久删除，结果状态：{status}",
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
    "library.scan.queued": _event(
        "library.scan.queued",
        LogCategory.LIBRARY,
        "媒体库扫描已排队",
        "媒体库扫描已排队，当前状态：{status}",
    ),
    "library.scan.completed": _event(
        "library.scan.completed",
        LogCategory.LIBRARY,
        "媒体库扫描已完成",
        "媒体库扫描已完成，读取 {items_seen} 个条目",
        fields=frozenset({"items_seen"}),
    ),
    "library.scan.failed": _event(
        "library.scan.failed",
        LogCategory.LIBRARY,
        "媒体库扫描失败",
        "媒体库扫描未完成，错误码：{error_code}",
        suggestion="请检查 115 目录读取状态和扫描断点后重试",
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
    "agent.token.auth_failed": _event(
        "agent.token.auth_failed",
        LogCategory.SECURITY,
        "Agent Token 认证失败",
        "Agent Token 认证失败，可能正在被暴力尝试",
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
    "subscription.check_failed": _event(
        "subscription.check_failed",
        LogCategory.SUBSCRIPTION,
        "订阅检查失败",
        "订阅检查失败，错误码：{error_code}",
        suggestion="请检查搜索服务状态，系统将在下一次检查时间自动重试",
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
    "workflow.discovery_verified": _event(
        "workflow.discovery_verified",
        LogCategory.TASK,
        "资源发现已确认",
        "已根据资源记录完成工作流发现阶段",
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
        "已停止待处理阶段，工作流状态：{status_zh}",
        fields=frozenset({"status_zh"}),
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
    "notify.delivered": _event(
        "notify.delivered",
        LogCategory.NOTIFICATION,
        "外部通知已送达",
        "外部通知已投递 {count} 个启用渠道",
        fields=frozenset({"count"}),
    ),
    "notify.delivery_failed": _event(
        "notify.delivery_failed",
        LogCategory.NOTIFICATION,
        "外部通知投递失败",
        "外部通知投递失败，错误码：{error_code}",
        suggestion="请检查飞书机器人 Webhook 配置后重试",
        fields=frozenset({"error_code"}),
    ),
    "webhook.test": _event(
        "webhook.test", LogCategory.NOTIFICATION, "Webhook 测试通知", "Webhook 测试通知已排队发送"
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
