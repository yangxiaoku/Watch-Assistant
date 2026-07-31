"""Centralized, safe API error descriptors for Web and automation clients."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ApiErrorDescriptor:
    code: str
    title_zh: str
    message_zh: str
    suggestion_zh: str
    retryable: bool
    action: str | None = None


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_CATALOG: dict[str, ApiErrorDescriptor] = {
    "tmdb_unavailable": ApiErrorDescriptor("tmdb_unavailable", "影视信息暂时无法加载", "本次影视资料没有更新。", "请重新加载影视资料。", True, "retry"),
    "pansou_unavailable": ApiErrorDescriptor("pansou_unavailable", "资源搜索暂时不可用", "本次资源查询没有完成，页面内容没有更新。", "请稍后重新搜索资源。", True, "retry"),
    "rate_limited": ApiErrorDescriptor("rate_limited", "请求过于频繁", "本次请求未执行，当前页面内容没有改变。", "请稍后再试。", True, "retry"),
    "settings_conflict": ApiErrorDescriptor("settings_conflict", "设置已在其他位置更新", "本次修改未保存，当前页面不是最新版本。", "请加载最新设置后重新提交。", False, "reload_settings"),
    "credential_rejected": ApiErrorDescriptor("credential_rejected", "凭据验证未通过", "新凭据未生效，原配置保持不变。", "请检查凭据后重新验证。", False, "reauthenticate"),
    "credential_validation_unavailable": ApiErrorDescriptor("credential_validation_unavailable", "暂时无法验证凭据", "本次验证未完成，原配置保持不变。", "请稍后重新验证。", True, "retry"),
    "needs_auth": ApiErrorDescriptor("needs_auth", "登录状态已失效", "任务尚未继续提交到远端。", "请前往凭据设置重新登录。", False, "reauthenticate"),
    "auth_not_configured": ApiErrorDescriptor("auth_not_configured", "登录服务暂时不可用", "本次登录未执行。", "请稍后重试或联系管理员。", True, "retry"),
    "uncertain": ApiErrorDescriptor("uncertain", "结果待确认", "远端可能已经接受，本次结果尚未确认。", "请先查看任务状态，不要重复提交。", False, "view_task"),
    "task_not_retryable": ApiErrorDescriptor("task_not_retryable", "当前任务不能直接重试", "原任务状态未改变。", "请查看任务状态后再决定下一步。", False, "view_task"),
    "task_not_cancellable": ApiErrorDescriptor("task_not_cancellable", "当前任务不能取消", "任务状态未改变，远端结果不会被伪造撤回。", "请查看任务状态；如果远端结果不确定，请先核对后再决定下一步。", False, "view_task"),
    "inspection_unsupported": ApiErrorDescriptor("inspection_unsupported", "当前环境无法检测资源", "未创建资源检测任务。", "请查看资源检测配置。", False, "inspect_configuration"),
    "request_failed": ApiErrorDescriptor("request_failed", "请求未完成", "本次请求未完成，当前页面没有更新。", "请检查输入和当前状态后再试。", False),
    "credentials_unavailable": ApiErrorDescriptor("credentials_unavailable", "连接配置暂时不可用", "连接配置没有更新。", "请稍后重新加载配置。", True, "retry"),
    "credential_unavailable": ApiErrorDescriptor("credential_unavailable", "凭据服务暂时不可用", "本次凭据操作未完成。", "请稍后重试。", True, "retry"),
    "invalid_credential_request": ApiErrorDescriptor("invalid_credential_request", "凭据请求有误", "本次凭据操作未执行。", "请检查提交的字段后再试。", False),
    "deployment_diagnostics_unavailable": ApiErrorDescriptor("deployment_diagnostics_unavailable", "部署诊断暂时不可用", "本次诊断未完成。", "请稍后重试。", True, "retry"),
    "backups_unavailable": ApiErrorDescriptor("backups_unavailable", "备份服务暂时不可用", "本次备份操作未完成。", "请稍后重试。", True, "retry"),
    "backup_requires_file_database": ApiErrorDescriptor("backup_requires_file_database", "当前数据库不可备份", "未创建备份文件。", "请使用文件数据库后再创建备份。", False),
    "backup_failed": ApiErrorDescriptor("backup_failed", "备份创建失败", "未创建完整备份文件。", "请检查磁盘空间后重试。", True, "retry"),
    "backup_not_found": ApiErrorDescriptor("backup_not_found", "备份不存在", "本次恢复预览未完成。", "请刷新备份列表后再试。", False, "retry"),
    "backup_manifest_invalid": ApiErrorDescriptor("backup_manifest_invalid", "备份清单无效", "该备份不能用于恢复。", "请选择其他备份。", False),
    "backup_database_missing": ApiErrorDescriptor("backup_database_missing", "备份文件缺失", "该备份不能用于恢复。", "请选择其他备份。", False),
    "invalid_backup_id": ApiErrorDescriptor("invalid_backup_id", "备份标识无效", "本次恢复预览未执行。", "请从备份列表重新选择。", False),
    "database_not_found": ApiErrorDescriptor("database_not_found", "数据库文件不存在", "未创建备份文件。", "请检查数据库配置后重试。", False),
    "manual_import_unavailable": ApiErrorDescriptor("manual_import_unavailable", "手动导入服务暂时不可用", "本次导入未创建。", "请稍后重试。", True, "retry"),
    "confirmation_required": ApiErrorDescriptor("confirmation_required", "需要确认操作", "本次操作未执行。", "请先完成预览并明确确认。", False),
    "resource_conflict": ApiErrorDescriptor("resource_conflict", "资源已存在", "本次导入未执行。", "请刷新资源列表后再试。", False, "refresh_snapshot"),
    "invalid_resource": ApiErrorDescriptor("invalid_resource", "资源内容无效", "本次导入未执行。", "请检查资源信息后再试。", False),
    "media_mismatch": ApiErrorDescriptor("media_mismatch", "资源与影视不匹配", "本次导入未执行。", "请确认资源对应的影视条目。", False),
    "invalid_magnet": ApiErrorDescriptor("invalid_magnet", "磁力链接无效", "本次导入未执行。", "请检查磁力链接后再试。", False),
    "unsupported_url": ApiErrorDescriptor("unsupported_url", "链接类型不受支持", "本次导入未执行。", "请提供支持的资源链接。", False),
    "invalid_share_url": ApiErrorDescriptor("invalid_share_url", "分享链接无效", "本次导入未执行。", "请检查分享链接后再试。", False),
    "unsupported_share_domain": ApiErrorDescriptor("unsupported_share_domain", "分享来源不受支持", "本次导入未执行。", "请提供支持的分享来源。", False),
    "season_metadata_unavailable": ApiErrorDescriptor("season_metadata_unavailable", "季集资料暂时不可用", "本次季集资料没有更新。", "请稍后重试。", True, "retry"),
    "season_not_found": ApiErrorDescriptor("season_not_found", "季集不存在", "本次季集资料未找到。", "请检查剧集和季数后再试。", False),
    "season_metadata_cache_invalid": ApiErrorDescriptor("season_metadata_cache_invalid", "季集缓存无效", "本次季集资料没有更新。", "请重新加载季集资料。", True, "retry"),
    "invalid_series_id": ApiErrorDescriptor("invalid_series_id", "剧集标识无效", "本次请求未执行。", "请从有效的剧集页面重新操作。", False),
    "invalid_season_number": ApiErrorDescriptor("invalid_season_number", "季数无效", "本次请求未执行。", "请检查季数后再试。", False),
    "invalid_language": ApiErrorDescriptor("invalid_language", "语言参数无效", "本次请求未执行。", "请检查语言设置后再试。", False),
    "inspection_not_found": ApiErrorDescriptor("inspection_not_found", "检测任务不存在", "本次请求未完成。", "请刷新检测任务列表后再试。", False, "retry"),
    "resource_not_inspectable": ApiErrorDescriptor("resource_not_inspectable", "资源不可检测", "未创建资源检测任务。", "请检查资源类型和检测配置。", False, "inspect_configuration"),
    "subscription_unavailable": ApiErrorDescriptor("subscription_unavailable", "订阅服务暂时不可用", "本次订阅操作未完成。", "请稍后重试。", True, "retry"),
    "subscription_not_found": ApiErrorDescriptor("subscription_not_found", "订阅不存在", "本次订阅操作未完成。", "请刷新订阅列表后再试。", False, "retry"),
    "subscription_exists": ApiErrorDescriptor("subscription_exists", "订阅已经存在", "本次订阅未创建。", "请查看现有订阅后再试。", False),
    "subscription_conflict": ApiErrorDescriptor("subscription_conflict", "订阅状态发生变化", "本次订阅操作未完成。", "请刷新订阅后再试。", False, "retry"),
    "subscription_not_pauseable": ApiErrorDescriptor("subscription_not_pauseable", "订阅当前不可暂停", "本次订阅操作未完成。", "请查看订阅当前状态。", False),
    "subscription_cancelled": ApiErrorDescriptor("subscription_cancelled", "订阅已经取消", "本次订阅操作未完成。", "请创建新的订阅。", False),
    "subscription_not_active": ApiErrorDescriptor("subscription_not_active", "订阅当前未激活", "本次订阅操作未完成。", "请查看订阅当前状态。", False),
    "subscription_check_failed": ApiErrorDescriptor("subscription_check_failed", "订阅检查失败", "本次订阅检查未完成。", "请稍后重试。", True, "retry"),
    "settings_unavailable": ApiErrorDescriptor("settings_unavailable", "设置服务暂时不可用", "设置没有更新。", "请稍后重试。", True, "retry"),
    "invalid_content_policy": ApiErrorDescriptor("invalid_content_policy", "内容策略有误", "本次设置未保存。", "请修正内容策略后再提交。", False),
    "p115_settings_unavailable": ApiErrorDescriptor("p115_settings_unavailable", "115 设置暂时不可用", "115 设置没有更新。", "请稍后重试。", True, "retry"),
    "p115_device_unavailable": ApiErrorDescriptor("p115_device_unavailable", "115 设备服务暂时不可用", "登录设备列表没有更新。", "请稍后重试。", True, "retry"),
    "p115_qrcode_unavailable": ApiErrorDescriptor("p115_qrcode_unavailable", "115 扫码服务暂时不可用", "本次二维码没有生成。", "请检查 115 连接后重试。", True, "retry"),
    "qrcode_provider_unavailable": ApiErrorDescriptor("qrcode_provider_unavailable", "115 二维码暂时不可用", "本次二维码请求没有完成。", "请稍后重新生成二维码。", True, "retry"),
    "qrcode_unavailable": ApiErrorDescriptor("qrcode_unavailable", "115 二维码无效", "本次二维码没有生成。", "请稍后重新生成二维码。", True, "retry"),
    "qrcode_session_not_found": ApiErrorDescriptor("qrcode_session_not_found", "二维码会话不存在", "本次扫码状态无法继续查询。", "请重新生成二维码。", False, "retry"),
    "qrcode_result_invalid": ApiErrorDescriptor("qrcode_result_invalid", "扫码结果无效", "本次登录没有保存。", "请重新扫码登录。", False, "reauthenticate"),
    "p115_qrcode_save_failed": ApiErrorDescriptor("p115_qrcode_save_failed", "扫码设备保存失败", "本次登录没有完成切换。", "请重新生成二维码后再试。", True, "retry"),
    "p115_directory_scope_unavailable": ApiErrorDescriptor("p115_directory_scope_unavailable", "115 目录范围未配置", "目录选择器无法打开。", "请先配置有效的 115 目标目录。", False, "inspect_configuration"),
    "p115_directory_out_of_scope": ApiErrorDescriptor("p115_directory_out_of_scope", "目录不在受管范围内", "本次目录读取被拒绝。", "请从已显示的目录进入选择。", False),
    "p115_directory_unavailable": ApiErrorDescriptor("p115_directory_unavailable", "115 目录服务暂时不可用", "目录选择器没有读取到内容。", "请检查 115 登录状态后重试。", True, "retry"),
    "p115_directory_read_failed": ApiErrorDescriptor("p115_directory_read_failed", "115 目录读取失败", "目录选择器没有读取到可靠内容。", "请重新验证 115 登录状态后重试。", True, "retry"),
    "active_device_cannot_revoke": ApiErrorDescriptor("active_device_cannot_revoke", "当前设备不能移除", "当前正在使用的登录设备未改变。", "请先切换到其他设备后再移除。", False),
    "device_not_found": ApiErrorDescriptor("device_not_found", "登录设备不存在", "本次设备操作未完成。", "请刷新设备列表后再试。", False, "retry"),
    "notifications_unavailable": ApiErrorDescriptor("notifications_unavailable", "通知服务暂时不可用", "本次通知操作未完成。", "请稍后重试。", True, "retry"),
    "webhooks_unavailable": ApiErrorDescriptor("webhooks_unavailable", "Webhook 服务暂时不可用", "本次 Webhook 操作未完成。", "请稍后重试。", True, "retry"),
    "webhook_not_found": ApiErrorDescriptor("webhook_not_found", "Webhook 端点不存在", "本次 Webhook 操作未完成。", "请刷新端点列表后再试。", False, "retry"),
    "webhook_conflict": ApiErrorDescriptor("webhook_conflict", "Webhook 端点已变化", "本次 Webhook 修改未保存。", "请刷新端点后重新提交。", False, "reload_settings"),
    "webhook_url_not_allowed": ApiErrorDescriptor("webhook_url_not_allowed", "Webhook 地址不受支持", "本次端点未保存。", "请使用允许的 HTTPS 公网地址。", False),
    "webhook_url_unresolvable": ApiErrorDescriptor("webhook_url_unresolvable", "Webhook 地址无法解析", "本次端点未保存。", "请检查域名后重试。", False),
    "webhook_event_not_allowed": ApiErrorDescriptor("webhook_event_not_allowed", "Webhook 事件不受支持", "本次端点未保存。", "请从已登记事件中选择。", False),
    "webhook_delivery_not_found": ApiErrorDescriptor("webhook_delivery_not_found", "Webhook 投递不存在", "本次投递操作未完成。", "请刷新投递记录后再试。", False, "retry"),
    "webhook_delivery_conflict": ApiErrorDescriptor("webhook_delivery_conflict", "Webhook 投递状态已变化", "本次投递操作未完成。", "请刷新投递记录后再试。", False, "retry"),
    "webhook_dns_failed": ApiErrorDescriptor("webhook_dns_failed", "Webhook 地址无法解析", "本次投递未发送。", "请检查接收端域名后重试。", True, "retry"),
    "pwa_unavailable": ApiErrorDescriptor("pwa_unavailable", "PWA 服务暂时不可用", "本次 PWA 操作未完成。", "请稍后重试。", True, "retry"),
    "pwa_device_not_found": ApiErrorDescriptor("pwa_device_not_found", "移动设备不存在", "本次设备操作未完成。", "请刷新设备列表后再试。", False, "retry"),
    "pwa_device_conflict": ApiErrorDescriptor("pwa_device_conflict", "移动设备已绑定其他账号", "本次设备未登记。", "请重新授权当前设备后再试。", False),
    "pwa_subscription_invalid": ApiErrorDescriptor("pwa_subscription_invalid", "推送订阅无效", "本次设备未登记。", "请重新开启通知权限后再试。", False),
    "notification_not_found": ApiErrorDescriptor("notification_not_found", "通知不存在", "本次通知操作未完成。", "请刷新通知列表后再试。", False, "retry"),
    "notification_preference_conflict": ApiErrorDescriptor("notification_preference_conflict", "通知设置已变化", "本次通知设置未保存。", "请刷新后重新提交。", False, "retry"),
    "quality_profiles_unavailable": ApiErrorDescriptor("quality_profiles_unavailable", "质量配置服务暂时不可用", "本次质量配置操作未完成。", "请稍后重试。", True, "retry"),
    "quality_profile_not_found": ApiErrorDescriptor("quality_profile_not_found", "质量配置不存在", "本次质量配置操作未完成。", "请刷新质量配置列表后再试。", False, "retry"),
    "quality_profile_conflict": ApiErrorDescriptor("quality_profile_conflict", "质量配置已变化", "本次质量配置未保存。", "请刷新后重新提交。", False, "reload_settings"),
    "invalid_quality_rules": ApiErrorDescriptor("invalid_quality_rules", "质量规则有误", "本次质量配置未保存。", "请修正质量规则后再提交。", False),
    "unknown_quality_rule": ApiErrorDescriptor("unknown_quality_rule", "质量规则不受支持", "本次质量配置未保存。", "请检查规则名称后再提交。", False),
    "invalid_min_resolution": ApiErrorDescriptor("invalid_min_resolution", "最低分辨率无效", "本次质量配置未保存。", "请使用支持的分辨率。", False),
    "invalid_filename": ApiErrorDescriptor("invalid_filename", "文件名无效", "本次字幕分析未完成。", "请检查视频文件名后再试。", False),
    "organization_plan_disabled": ApiErrorDescriptor("organization_plan_disabled", "整理计划功能未启用", "本次整理计划操作未执行。", "请查看整理功能配置。", False, "inspect_configuration"),
    "library_not_found": ApiErrorDescriptor("library_not_found", "媒体库不存在", "本次媒体库查询未完成。", "请刷新媒体库列表后再试。", False, "refresh_snapshot"),
    "library_configuration_conflict": ApiErrorDescriptor("library_configuration_conflict", "媒体库配置已变化", "本次媒体库配置未保存。", "请刷新媒体库配置后重新提交。", False, "reload_settings"),
    "library_scope_unavailable": ApiErrorDescriptor("library_scope_unavailable", "媒体库范围暂不可配置", "本次媒体库配置未保存，服务端没有可验证的 115 目标范围。", "请检查 P115 目标目录配置后重试。", False, "inspect_configuration"),
    "library_scope_mismatch": ApiErrorDescriptor("library_scope_mismatch", "媒体库范围不匹配", "本次媒体库配置未保存，目录不在服务端声明的 115 目标范围内。", "请使用服务端已配置的目标目录。", False),
    "library_scope_verification_failed": ApiErrorDescriptor("library_scope_verification_failed", "媒体库范围验证失败", "本次媒体库未启用，115 目录范围没有得到可靠验证。", "请确认凭据有效并稍后重新验证。", True, "retry"),
    "library_scope_unverified": ApiErrorDescriptor("library_scope_unverified", "媒体库范围尚未验收", "本次扫描未执行，媒体库范围还没有完成只读验证。", "请先完成媒体库范围验证。", False, "inspect_configuration"),
    "media_not_found": ApiErrorDescriptor("media_not_found", "媒体条目不存在", "本次媒体条目查询未完成。", "请刷新媒体库索引后再试。", False, "refresh_snapshot"),
    "library_inventory_incomplete": ApiErrorDescriptor("library_inventory_incomplete", "库存索引不完整", "本次身份确认未保存。", "请先完成一次完整库存扫描后再试。", False, "refresh_snapshot"),
    "library_identity_conflict": ApiErrorDescriptor("library_identity_conflict", "库存身份已变化", "本次身份确认未保存。", "请刷新库存后重新确认。", False, "refresh_snapshot"),
    "audit_not_found": ApiErrorDescriptor("audit_not_found", "审计记录不存在", "本次审计查询未完成。", "请刷新审计列表后再试。", False, "refresh_snapshot"),
    "organization_plan_unavailable": ApiErrorDescriptor("organization_plan_unavailable", "整理计划服务暂时不可用", "本次整理计划操作未完成。", "请稍后重试。", True, "retry"),
    "organization_preview_unavailable": ApiErrorDescriptor("organization_preview_unavailable", "整理预览服务暂时不可用", "本次整理预览未完成。", "请稍后重试。", True, "retry"),
    "target_catalog_unavailable": ApiErrorDescriptor("target_catalog_unavailable", "目标目录暂时无法读取", "本次整理预览未完成，目标目录清单没有得到可靠验证。", "请检查 115 连接和目标目录后重试。", True, "retry"),
    "organization_execution_disabled": ApiErrorDescriptor("organization_execution_disabled", "整理执行功能未启用", "本次整理操作未执行。", "请查看整理功能配置。", False, "inspect_configuration"),
    "high_risk_approval_required": ApiErrorDescriptor("high_risk_approval_required", "需要 Web 人工批准", "影响数量超过阈值，本次整理未排队。", "请在任务中心完成人工批准后再提交。", False, "view_task"),
    "web_approval_required": ApiErrorDescriptor("web_approval_required", "需要 Web 人工批准", "Agent 不能直接批准高风险计划。", "请使用已登录的 Web 会话完成批准。", False, "view_task"),
    "organization_write_disabled": ApiErrorDescriptor("organization_write_disabled", "真实整理写入未启用", "本次 115 写入操作未执行。", "请先检查真实写入开关。", False, "inspect_configuration"),
    "organization_write_unverified": ApiErrorDescriptor("organization_write_unverified", "真实整理写入契约未验收", "本次 115 写入操作未执行。", "请先完成受管测试目录的写入契约验收。", False, "inspect_configuration"),
    "permanent_delete_disabled": ApiErrorDescriptor("permanent_delete_disabled", "永久删除未启用", "本次删除操作未执行。", "请先检查永久删除开关。", False, "inspect_configuration"),
    "permanent_delete_unverified": ApiErrorDescriptor("permanent_delete_unverified", "永久删除契约未验收", "本次删除操作未执行。", "删除契约未完成前不能执行真实删除。", False, "inspect_configuration"),
    "delete_unavailable": ApiErrorDescriptor("delete_unavailable", "删除服务暂时不可用", "本次删除操作未完成。", "请稍后重试。", True, "retry"),
    "strm_full_disabled": ApiErrorDescriptor("strm_full_disabled", "STRM 功能未启用", "本次 STRM 操作未执行。", "请查看 STRM 功能配置。", False, "inspect_configuration"),
    "strm_incremental_disabled": ApiErrorDescriptor("strm_incremental_disabled", "STRM 增量同步未启用", "本次 STRM 增量操作未执行。", "请查看 STRM 功能配置。", False, "inspect_configuration"),
    "strm_cleanup_disabled": ApiErrorDescriptor("strm_cleanup_disabled", "STRM 失效清理未启用", "本次 STRM 清理未执行。", "请查看 STRM 功能配置。", False, "inspect_configuration"),
    "strm_unavailable": ApiErrorDescriptor("strm_unavailable", "STRM 服务暂时不可用", "本次 STRM 操作未完成。", "请稍后重试。", True, "retry"),
    "source_snapshot_not_ready": ApiErrorDescriptor("source_snapshot_not_ready", "扫描快照尚未就绪", "本次 STRM 清理计划未生成。", "请先完成一次完整且受保护的媒体库扫描。", False, "refresh_snapshot"),
    "source_snapshot_not_current": ApiErrorDescriptor("source_snapshot_not_current", "扫描快照已过期", "本次 STRM 清理计划未生成。", "请刷新媒体库后重新生成计划。", False, "refresh_snapshot"),
    "strm_output_unavailable": ApiErrorDescriptor("strm_output_unavailable", "STRM 输出目录不可用", "本次 STRM 清理计划未生成。", "请检查受管 STRM 输出目录。", False, "inspect_configuration"),
    "invalid_playback_url_prefix": ApiErrorDescriptor("invalid_playback_url_prefix", "STRM 播放入口配置无效", "本次 STRM 清理计划未生成。", "请检查稳定播放入口配置。", False, "inspect_configuration"),
    "plan_invalid": ApiErrorDescriptor("plan_invalid", "STRM 清理计划无效", "本次 STRM 清理计划无法读取。", "请重新生成清理计划。", False, "refresh_snapshot"),
    "invalid_playback_request": ApiErrorDescriptor("invalid_playback_request", "播放请求无效", "本次播放请求未执行。", "请重新打开受管 STRM 文件。", False),
    "strm_playback_disabled": ApiErrorDescriptor("strm_playback_disabled", "STRM 播放未启用", "本次播放请求未执行。", "请检查 STRM 播放配置。", False, "inspect_configuration"),
    "strm_playback_unverified": ApiErrorDescriptor("strm_playback_unverified", "STRM 播放契约未验收", "本次播放请求未执行。", "请先完成播放契约验收。", False, "inspect_configuration"),
    "strm_playback_unavailable": ApiErrorDescriptor("strm_playback_unavailable", "STRM 播放服务暂不可用", "本次播放请求未完成。", "请检查 115 连接和播放配置。", True, "retry"),
    "playback_file_not_found": ApiErrorDescriptor("playback_file_not_found", "媒体文件不存在", "受管媒体文件当前不可播放。", "请重新扫描媒体库并更新 STRM。", False, "refresh_snapshot"),
    "playback_network_forbidden": ApiErrorDescriptor("playback_network_forbidden", "当前网络不允许播放", "本次播放请求未执行。", "请从已允许的局域网或 Tailscale 网络访问。", False),
    "playback_timeout": ApiErrorDescriptor("playback_timeout", "播放连接超时", "本次播放连接没有完成。", "请稍后重试。", True, "retry"),
    "playback_remote_failed": ApiErrorDescriptor("playback_remote_failed", "远端播放连接失败", "本次播放连接没有完成。", "请检查 115 文件状态后重试。", True, "retry"),
    "organization_operation_unavailable": ApiErrorDescriptor("organization_operation_unavailable", "整理操作服务暂时不可用", "本次整理操作未完成。", "请稍后重试。", True, "retry"),
    "organization_schedule_unavailable": ApiErrorDescriptor("organization_schedule_unavailable", "整理调度服务暂时不可用", "本次整理没有排队。", "请检查 115 整理执行能力后重试。", True, "retry"),
    "source_target_same": ApiErrorDescriptor("source_target_same", "整理目录配置有误", "源目录和目标目录不能相同。", "请填写不同的 115 目录。", False),
    "invalid_source_directory_ids": ApiErrorDescriptor("invalid_source_directory_ids", "源目录 ID 无效", "本次整理设置未保存。", "请填写纯数字的 115 目录 CID。", False),
    "invalid_target_directory_id": ApiErrorDescriptor("invalid_target_directory_id", "目标目录 ID 无效", "本次整理设置未保存。", "请填写纯数字的 115 目录 CID。", False),
    "invalid_page": ApiErrorDescriptor("invalid_page", "页码无效", "本次目录读取未执行。", "请重新打开目录选择器。", False),
    "invalid_video_extensions": ApiErrorDescriptor("invalid_video_extensions", "视频扩展名无效", "本次整理设置未保存。", "请使用不带点号的文件扩展名。", False),
    "invalid_metadata_extensions": ApiErrorDescriptor("invalid_metadata_extensions", "元数据扩展名无效", "本次整理设置未保存。", "请使用不带点号的文件扩展名。", False),
    "plan_not_found": ApiErrorDescriptor("plan_not_found", "整理计划不存在", "本次整理操作未完成。", "请刷新整理计划列表后再试。", False, "retry"),
    "invalid_plan": ApiErrorDescriptor("invalid_plan", "整理计划无效", "本次整理操作未执行。", "请刷新整理计划后再试。", False),
    "invalid_pagination": ApiErrorDescriptor("invalid_pagination", "分页参数无效", "本次请求未执行。", "请检查分页参数后再试。", False),
    "invalid_revision": ApiErrorDescriptor("invalid_revision", "版本参数无效", "本次整理操作未执行。", "请刷新后再提交。", False),
    "invalid_alias": ApiErrorDescriptor("invalid_alias", "别名格式无效", "本次整理计划未保存。", "请检查别名后再提交。", False),
    "stale_revision": ApiErrorDescriptor("stale_revision", "计划版本已变化", "本次整理计划未保存。", "请刷新后重新提交。", False, "reload_settings"),
    "plan_not_reviewable": ApiErrorDescriptor("plan_not_reviewable", "计划当前不可修改", "本次整理计划未保存。", "请查看计划当前状态。", False),
    "operation_not_found": ApiErrorDescriptor("operation_not_found", "整理操作不存在", "本次整理操作未完成。", "请刷新操作列表后再试。", False, "retry"),
    "operation_unavailable": ApiErrorDescriptor("operation_unavailable", "整理操作暂不可用", "本次整理操作未完成。", "请稍后重试。", True, "retry"),
    "plan_digest_required": ApiErrorDescriptor("plan_digest_required", "缺少计划摘要", "本次整理操作未执行。", "请提供当前计划摘要后再试。", False),
    "plan_digest_mismatch": ApiErrorDescriptor("plan_digest_mismatch", "计划摘要已变化", "本次整理操作未执行。", "请刷新计划并重新确认。", False, "reload_settings"),
    "cleanup_plan_expired": ApiErrorDescriptor("cleanup_plan_expired", "STRM 清理计划已过期", "本次 STRM 清理未执行。", "请重新生成清理计划并确认。", False, "refresh_snapshot"),
    "cleanup_plan_blocked": ApiErrorDescriptor("cleanup_plan_blocked", "STRM 清理候选已被修改", "本次 STRM 清理未执行，避免误删用户内容。", "请重新扫描并生成清理计划。", False, "refresh_snapshot"),
    "cleanup_plan_changed": ApiErrorDescriptor("cleanup_plan_changed", "STRM 清单已变化", "本次 STRM 清理未执行。", "请重新生成清理计划后再确认。", False, "refresh_snapshot"),
    "cleanup_plan_not_reviewable": ApiErrorDescriptor("cleanup_plan_not_reviewable", "STRM 清理计划不可执行", "本次 STRM 清理未执行。", "请查看计划当前状态。", False, "view_task"),
    "invalid_plan_id": ApiErrorDescriptor("invalid_plan_id", "计划标识无效", "本次整理操作未执行。", "请刷新后再试。", False),
    "invalid_idempotency_key": ApiErrorDescriptor("invalid_idempotency_key", "幂等标识无效", "本次整理操作未执行。", "请重新提交有效的幂等标识。", False),
    "invalid_operation_id": ApiErrorDescriptor("invalid_operation_id", "操作标识无效", "本次整理操作未执行。", "请刷新后再试。", False),
    "plan_is_not_planned": ApiErrorDescriptor("plan_is_not_planned", "计划尚未确认或已不可用", "本次整理操作未执行。", "请确认计划当前状态。", False),
    "plan_prerequisites_changed": ApiErrorDescriptor("plan_prerequisites_changed", "计划前置条件已变化", "本次整理操作未执行。", "请重新确认计划。", False, "reload_settings"),
    "plan_revision_changed": ApiErrorDescriptor("plan_revision_changed", "计划版本已变化", "本次整理操作未执行。", "请刷新后重新提交。", False, "reload_settings"),
    "idempotency_key_conflict": ApiErrorDescriptor("idempotency_key_conflict", "幂等请求发生冲突", "本次整理操作未执行。", "请查看已有操作后再试。", False, "view_task"),
    "plan_already_has_operation": ApiErrorDescriptor("plan_already_has_operation", "计划已有受控操作", "本次整理操作未创建。", "请查看已有操作状态。", False, "view_task"),
    "operation_plan_conflict": ApiErrorDescriptor("operation_plan_conflict", "计划操作发生冲突", "本次整理操作未创建。", "请刷新后再试。", False, "retry"),
    "operation_creation_conflict": ApiErrorDescriptor("operation_creation_conflict", "操作创建发生冲突", "本次整理操作未创建。", "请刷新后再试。", False, "retry"),
    "operation_revision_changed": ApiErrorDescriptor("operation_revision_changed", "操作版本已变化", "本次整理操作未完成。", "请刷新后再试。", False, "reload_settings"),
    "operation_is_not_cancellable": ApiErrorDescriptor("operation_is_not_cancellable", "操作当前不可取消", "本次取消操作未执行。", "请查看操作当前状态。", False),
    "uncertain_requires_verification": ApiErrorDescriptor("uncertain_requires_verification", "操作结果待确认", "系统不会重复提交该操作。", "请先查看远端任务状态。", False, "view_task"),
    "operation_is_not_claimable": ApiErrorDescriptor("operation_is_not_claimable", "操作当前不可领取", "本次执行未开始。", "请查看操作当前状态。", False),
    "lease_is_active": ApiErrorDescriptor("lease_is_active", "操作正在由其他执行器处理", "本次执行未开始。", "请稍后查看操作状态。", True, "retry"),
    "lease_claim_lost": ApiErrorDescriptor("lease_claim_lost", "操作执行权已变化", "本次执行已停止继续提交。", "请查看操作状态后再试。", False, "view_task"),
    "lease_is_not_owned": ApiErrorDescriptor("lease_is_not_owned", "当前执行器没有操作执行权", "本次执行未完成。", "请查看操作状态。", False, "view_task"),
    "operation_is_not_retryable": ApiErrorDescriptor("operation_is_not_retryable", "当前操作不能重试", "原操作状态未改变。", "请查看操作状态后再决定下一步。", False, "view_task"),
    "invalid_terminal_status": ApiErrorDescriptor("invalid_terminal_status", "终态无效", "本次操作状态未更新。", "请查看操作状态。", False),
    "directory_scope_required": ApiErrorDescriptor("directory_scope_required", "缺少目录范围", "本次整理操作未执行。", "请补充目录范围后再试。", False),
    "cache_warm_disabled": ApiErrorDescriptor("cache_warm_disabled", "缓存预热功能未启用", "本次缓存操作未执行。", "请查看缓存预热配置。", False, "inspect_configuration"),
    "cache_warm_running": ApiErrorDescriptor("cache_warm_running", "缓存预热正在进行", "本次重试未创建。", "请等待当前预热完成。", False),
    "resource_not_found": ApiErrorDescriptor("resource_not_found", "资源不存在", "本次任务未创建，资源可能已被移除。", "请刷新资源列表后再试。", False, "refresh_snapshot"),
    "push_kind_unsupported": ApiErrorDescriptor("push_kind_unsupported", "当前资源类型不可推送", "本次任务未创建。", "请查看 115 推送能力配置。", False, "inspect_configuration"),
    "inventory_scope_unconfigured": ApiErrorDescriptor("inventory_scope_unconfigured", "媒体库库存范围未配置", "本次远端提交已阻止，系统无法确认媒体库中是否已有该资源。", "先配置媒体库并完成一次完整扫描，再重试任务。", False, "refresh_snapshot"),
    "inventory_index_incomplete": ApiErrorDescriptor("inventory_index_incomplete", "媒体库库存索引不完整", "本次远端提交已阻止，系统无法确认媒体库中是否已有该资源。", "先完成一次完整库存扫描，再重试任务。", False, "refresh_snapshot"),
    "inventory_index_stale": ApiErrorDescriptor("inventory_index_stale", "媒体库库存索引已过期", "本次远端提交已阻止，系统无法确认媒体库中是否已有该资源。", "刷新媒体库库存后再重试任务。", False, "refresh_snapshot"),
    "inventory_index_unknown": ApiErrorDescriptor("inventory_index_unknown", "媒体库库存状态未知", "本次远端提交已阻止，系统无法确认媒体库中是否已有该资源。", "刷新媒体库库存并确认扫描时间后再试。", False, "refresh_snapshot"),
    "inventory_exact_duplicate": ApiErrorDescriptor("inventory_exact_duplicate", "资源已在媒体库中", "本次远端提交已阻止，媒体库已有相同资源。", "查看媒体库现有版本，不要重复提交。", False, "refresh_snapshot"),
    "inventory_review_required": ApiErrorDescriptor("inventory_review_required", "媒体库存在相近资源", "本次远端提交已阻止，媒体库存在相同媒体或待确认版本。", "先查看库存版本并完成人工确认，再决定是否继续。", False, "refresh_snapshot"),
    "inventory_check_failed": ApiErrorDescriptor("inventory_check_failed", "库存检查未完成", "本次远端提交已阻止，库存检查没有得到可靠结果。", "刷新库存后再重试；不要重复提交可能已在途的任务。", True, "retry"),
    "task_not_found": ApiErrorDescriptor("task_not_found", "任务不存在", "本次任务操作未完成。", "请刷新任务列表后再试。", False, "retry"),
    "workflow_not_found": ApiErrorDescriptor("workflow_not_found", "关联工作流不存在", "本次任务未创建。", "请刷新页面后重新操作。", False, "retry"),
    "workflow_id_conflict": ApiErrorDescriptor("workflow_id_conflict", "任务已关联其他工作流", "本次任务未改动现有关联。", "请查看原工作流或创建新的搜索任务。", False, "view_task"),
    "workflows_unavailable": ApiErrorDescriptor("workflows_unavailable", "工作流服务暂时不可用", "本次工作流操作未完成。", "请稍后重试。", True, "retry"),
    "workflow_conflict": ApiErrorDescriptor("workflow_conflict", "工作流状态发生冲突", "本次工作流操作未执行。", "请刷新工作流状态后再试。", False, "reload_settings"),
    "workflow_not_awaiting_confirmation": ApiErrorDescriptor("workflow_not_awaiting_confirmation", "工作流当前不需要确认", "本次确认未执行。", "请查看当前阶段状态后再操作。", False, "view_task"),
    "workflow_not_cancellable": ApiErrorDescriptor("workflow_not_cancellable", "工作流当前不可取消", "本次取消未执行，运行中或远端结果不会被伪造撤回。", "请先查看工作流阶段和远端任务状态。", False, "view_task"),
    "season_requires_tv": ApiErrorDescriptor("season_requires_tv", "季集参数仅适用于剧集", "本次请求未执行。", "请选择剧集后再查看季集。", False),
    "invalid_page_size": ApiErrorDescriptor("invalid_page_size", "分页大小无效", "本次请求未执行。", "请使用支持的分页大小。", False),
    "resource_snapshot_not_found": ApiErrorDescriptor("resource_snapshot_not_found", "资源列表已更新", "当前分页快照已失效。", "请返回第一页刷新资源。", True, "refresh_snapshot"),
    "resource_search_not_found": ApiErrorDescriptor("resource_search_not_found", "资源搜索任务不存在", "本次资源查询状态未找到。", "请重新查找资源。", False, "retry"),
    "resource_search_unavailable": ApiErrorDescriptor("resource_search_unavailable", "资源搜索暂时不可用", "本次资源查询未完成。", "请稍后重试。", True, "retry"),
    "resource_search_failed": ApiErrorDescriptor("resource_search_failed", "资源搜索失败", "本次资源查询未完成。", "请稍后重新查找资源。", True, "retry"),
    "resource_search_timeout": ApiErrorDescriptor("resource_search_timeout", "资源搜索超时", "本次等待已结束，服务端任务仍会继续运行。", "请稍后刷新资源状态，或重新打开详情。", True, "retry"),
    "invalid_season_request": ApiErrorDescriptor("invalid_season_request", "季集请求无效", "本次资源查询未执行。", "请检查剧集和季数后再试。", False),
    "validation_error": ApiErrorDescriptor("validation_error", "输入内容有误", "本次请求未执行，页面内容没有改变。", "请修正标记的字段后再提交。", False),
    "unauthorized": ApiErrorDescriptor("unauthorized", "需要登录", "本次请求未执行。", "请先登录后再试。", False, "reauthenticate"),
    "forbidden": ApiErrorDescriptor("forbidden", "没有执行权限", "本次请求未执行。", "请检查当前账号的权限范围。", False),
    "missing_scope": ApiErrorDescriptor("missing_scope", "Agent 权限不足", "本次请求未执行。", "请为当前 Agent Token 增加所需权限后重试。", False),
    "resource_forbidden": ApiErrorDescriptor("resource_forbidden", "资源范围不允许", "本次请求未执行，当前 Agent 无权访问该资源。", "请使用已授权的媒体库范围后再试。", False),
    "invalid_request": ApiErrorDescriptor("invalid_request", "请求格式有误", "本次请求未执行。", "请检查请求字段和参数后再试。", False),
    "method_not_found": ApiErrorDescriptor("method_not_found", "操作不受支持", "本次请求未执行，当前接口不支持该方法。", "请查看当前版本支持的 MCP 方法。", False),
    "tool_not_found": ApiErrorDescriptor("tool_not_found", "工具不存在", "本次请求未执行，当前工具未在允许列表中。", "请先读取工具列表后再试。", False),
    "mcp_unavailable": ApiErrorDescriptor("mcp_unavailable", "自动化接口暂时不可用", "本次 MCP 请求未执行。", "请稍后重试；如果问题持续，请检查服务状态。", True, "retry"),
    "agent_token_not_found": ApiErrorDescriptor("agent_token_not_found", "Agent Token 不存在", "本次操作未完成。", "请刷新 Agent 管理列表后重试。", False),
    "agent_token_conflict": ApiErrorDescriptor("agent_token_conflict", "Agent Token 状态已变化", "本次操作未完成。", "请刷新 Agent 管理列表后重试。", False),
    "invalid_agent_token_request": ApiErrorDescriptor("invalid_agent_token_request", "Agent Token 请求有误", "本次操作未执行。", "请检查名称、权限和有效期后重试。", False),
    "not_found": ApiErrorDescriptor("not_found", "内容不存在", "本次请求未完成，目标内容可能已被移除。", "请刷新页面后再试。", False, "retry"),
    "internal_error": ApiErrorDescriptor("internal_error", "服务暂时无法完成操作", "本次操作未完成，当前页面没有更新。", "请稍后重试；如果问题持续，请提供请求 ID。", True, "retry"),
}


def catalog_codes() -> frozenset[str]:
    """Return the immutable set used by API and automation contract tests."""

    return frozenset(_CATALOG)


def error_code_from_detail(detail: object, status_code: int) -> str:
    candidate: object = detail
    if isinstance(detail, Mapping):
        candidate = detail.get("code")
    if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate):
        return candidate
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 422:
        return "validation_error"
    if status_code >= 500:
        return "internal_error"
    return "request_failed"


def build_error_payload(
    code: str,
    status_code: int,
    *,
    request_id: str,
    correlation_id: str,
    field_errors: list[dict[str, str]] | None = None,
    missing_scopes: list[str] | None = None,
) -> dict[str, Any]:
    descriptor = _CATALOG.get(code)
    if descriptor is None:
        retryable = status_code in {408, 429} or status_code >= 500
        descriptor = ApiErrorDescriptor(
            code,
            "操作暂时无法完成",
            "本次操作未完成，当前页面没有更新。",
            "请稍后重试；如果问题持续，请提供请求 ID。" if retryable else "请检查输入和当前状态后再试。",
            retryable,
            "retry" if retryable else None,
        )
    payload = {
        "code": descriptor.code,
        "title_zh": descriptor.title_zh,
        "message_zh": descriptor.message_zh,
        "suggestion_zh": descriptor.suggestion_zh,
        "retryable": descriptor.retryable,
        "action": descriptor.action,
        "field_errors": field_errors or [],
        "request_id": request_id,
        "correlation_id": correlation_id,
    }
    if missing_scopes:
        payload["missing_scopes"] = sorted(set(missing_scopes))
    return payload


def legacy_detail(detail: object, code: str) -> object:
    """Keep the old field only for compatibility, without exposing exceptions."""

    if isinstance(detail, str) and _SAFE_CODE.fullmatch(detail):
        return detail
    if isinstance(detail, Mapping) and isinstance(detail.get("code"), str):
        return dict(detail)
    return code
