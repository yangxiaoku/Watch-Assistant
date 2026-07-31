export type UiErrorAction =
  | "retry"
  | "reload_settings"
  | "reauthenticate"
  | "view_task"
  | "refresh_snapshot"
  | "inspect_configuration"
  | "open_library"
  | null;

export interface UiErrorDescriptor {
  code: string;
  title: string;
  message: string;
  suggestion: string;
  retryable: boolean;
  action: UiErrorAction;
}

const RETRYABLE_CODES = new Set([
  "auth_not_configured",
  "backups_unavailable",
  "backup_failed",
  "credential_unavailable",
  "credentials_unavailable",
  "deployment_diagnostics_unavailable",
  "internal_error",
  "manual_import_unavailable",
  "notifications_unavailable",
  "operation_unavailable",
  "organization_plan_unavailable",
  "organization_preview_unavailable",
  "target_catalog_unavailable",
  "organization_operation_unavailable",
  "organization_schedule_unavailable",
  "p115_settings_unavailable",
  "qrcode_provider_unavailable",
  "quality_profiles_unavailable",
  "season_metadata_unavailable",
  "settings_unavailable",
  "subscription_check_failed",
  "subscription_unavailable",
  "tmdb_unavailable",
  "not_found",
  "resource_search_unavailable",
  "resource_search_failed",
  "resource_search_timeout",
  "workflows_unavailable",
  "library_scope_verification_failed",
  "library_scope_unverified",
]);

const ACTION_BY_CODE: Record<string, UiErrorAction> = {
  backup_not_found: "retry",
  cache_warm_disabled: "inspect_configuration",
  idempotency_key_conflict: "view_task",
  inspection_not_found: "retry",
  library_configuration_conflict: "reload_settings",
  library_scope_mismatch: "inspect_configuration",
  library_scope_unavailable: "inspect_configuration",
  library_scope_verification_failed: "retry",
  target_catalog_unavailable: "retry",
  inventory_scope_unconfigured: "open_library",
  inventory_index_incomplete: "open_library",
  inventory_index_stale: "open_library",
  inventory_index_unknown: "open_library",
  inventory_exact_duplicate: "open_library",
  inventory_review_required: "open_library",
  lease_claim_lost: "view_task",
  lease_is_not_owned: "view_task",
  operation_is_not_retryable: "view_task",
  operation_not_found: "retry",
  operation_plan_conflict: "retry",
  organization_plan_disabled: "inspect_configuration",
  organization_execution_disabled: "inspect_configuration",
  organization_write_disabled: "inspect_configuration",
  organization_write_unverified: "inspect_configuration",
  permanent_delete_disabled: "inspect_configuration",
  permanent_delete_unverified: "inspect_configuration",
  strm_full_disabled: "inspect_configuration",
  strm_incremental_disabled: "inspect_configuration",
  strm_cleanup_disabled: "inspect_configuration",
  plan_already_has_operation: "view_task",
  plan_prerequisites_changed: "reload_settings",
  plan_revision_changed: "reload_settings",
  push_kind_unsupported: "inspect_configuration",
  quality_profile_conflict: "reload_settings",
  quality_profile_not_found: "retry",
  resource_not_found: "refresh_snapshot",
  resource_snapshot_not_found: "refresh_snapshot",
  stale_revision: "reload_settings",
  subscription_conflict: "retry",
  subscription_not_found: "retry",
  task_not_found: "retry",
  uncertain_requires_verification: "view_task",
  workflow_not_found: "retry",
  resource_search_not_found: "retry",
  resource_search_unavailable: "retry",
  resource_search_failed: "retry",
  resource_search_timeout: "retry",
};

const ADDITIONAL_CODES = [
  "auth_not_configured", "internal_error", "not_found", "backup_requires_file_database", "backup_failed", "backup_not_found",
  "backup_manifest_invalid", "backup_database_missing", "invalid_backup_id", "database_not_found",
  "backups_unavailable", "credential_unavailable", "credentials_unavailable", "invalid_credential_request",
  "deployment_diagnostics_unavailable", "manual_import_unavailable", "confirmation_required",
  "resource_conflict", "invalid_resource", "media_mismatch", "invalid_magnet", "unsupported_url",
  "invalid_share_url", "unsupported_share_domain", "season_metadata_unavailable", "season_not_found",
  "season_metadata_cache_invalid", "invalid_series_id", "invalid_season_number", "invalid_language",
  "inspection_not_found", "resource_not_inspectable", "subscription_unavailable", "subscription_not_found",
  "subscription_exists", "subscription_conflict", "subscription_not_pauseable", "subscription_cancelled",
  "subscription_not_active", "subscription_check_failed", "settings_unavailable", "invalid_content_policy",
  "p115_settings_unavailable", "notifications_unavailable", "notification_not_found",
  "notification_preference_conflict", "quality_profiles_unavailable", "quality_profile_not_found",
  "quality_profile_conflict", "invalid_quality_rules", "unknown_quality_rule", "invalid_min_resolution",
  "invalid_filename", "organization_plan_disabled", "organization_plan_unavailable", "organization_preview_unavailable",
  "target_catalog_unavailable",
  "organization_schedule_unavailable", "source_target_same", "invalid_source_directory_ids", "invalid_target_directory_id",
  "invalid_video_extensions", "invalid_metadata_extensions",
  "organization_execution_disabled", "organization_write_disabled", "organization_write_unverified", "permanent_delete_disabled", "permanent_delete_unverified", "delete_unavailable", "strm_full_disabled", "strm_incremental_disabled", "strm_cleanup_disabled", "strm_unavailable", "source_snapshot_not_ready", "source_snapshot_not_current", "strm_output_unavailable", "invalid_playback_url_prefix", "plan_invalid", "cleanup_plan_expired", "cleanup_plan_blocked", "cleanup_plan_changed", "cleanup_plan_not_reviewable", "organization_operation_unavailable", "plan_not_found",
  "invalid_plan", "invalid_pagination", "invalid_revision", "invalid_alias", "stale_revision",
  "plan_not_reviewable", "operation_not_found", "operation_unavailable", "invalid_plan_id",
  "invalid_idempotency_key", "invalid_operation_id", "plan_is_not_planned", "plan_prerequisites_changed",
  "plan_revision_changed", "idempotency_key_conflict", "plan_already_has_operation", "operation_plan_conflict",
  "operation_creation_conflict", "operation_revision_changed", "operation_is_not_cancellable",
  "uncertain_requires_verification", "operation_is_not_claimable", "lease_is_active", "lease_claim_lost",
  "lease_is_not_owned", "operation_is_not_retryable", "invalid_terminal_status", "directory_scope_required",
  "cache_warm_disabled", "cache_warm_running", "resource_not_found", "push_kind_unsupported", "task_not_found",
  "workflow_not_found", "workflows_unavailable", "season_requires_tv", "invalid_page_size",
  "workflow_id_conflict",
  "workflow_conflict", "workflow_not_awaiting_confirmation", "workflow_not_cancellable", "high_risk_approval_required", "web_approval_required",
  "resource_snapshot_not_found", "resource_search_not_found", "resource_search_unavailable",
  "resource_search_failed", "invalid_season_request", "library_inventory_incomplete",
  "library_identity_conflict",
  "p115_device_unavailable", "p115_directory_out_of_scope", "p115_directory_read_failed",
  "p115_directory_scope_unavailable", "p115_directory_unavailable", "p115_qrcode_save_failed",
  "p115_qrcode_unavailable", "qrcode_provider_unavailable", "qrcode_result_invalid",
  "qrcode_session_not_found", "qrcode_unavailable", "active_device_cannot_revoke",
  "device_not_found", "invalid_page",
  "agent_token_conflict", "agent_token_not_found", "audit_not_found", "forbidden",
  "inspection_unsupported", "invalid_agent_token_request", "invalid_request",
  "inventory_check_failed", "inventory_exact_duplicate", "inventory_index_incomplete",
  "inventory_index_stale", "inventory_index_unknown", "inventory_review_required",
  "inventory_scope_unconfigured", "library_not_found", "mcp_unavailable", "media_not_found",
  "method_not_found", "missing_scope", "needs_auth", "pansou_unavailable",
  "plan_digest_mismatch", "plan_digest_required", "pwa_device_conflict", "pwa_device_not_found",
  "pwa_subscription_invalid", "pwa_unavailable", "rate_limited", "request_failed",
  "resource_forbidden", "resource_search_timeout", "settings_conflict", "task_not_retryable", "task_not_cancellable",
  "tmdb_unavailable", "tool_not_found", "unauthorized", "uncertain", "validation_error",
  "library_configuration_conflict", "library_scope_mismatch", "library_scope_unavailable",
  "library_scope_verification_failed",
  "library_scope_unverified",
  "invalid_playback_request", "strm_playback_disabled", "strm_playback_unverified",
  "strm_playback_unavailable", "playback_file_not_found", "playback_network_forbidden",
  "playback_timeout", "playback_remote_failed",
  "webhook_conflict", "webhook_delivery_conflict", "webhook_delivery_not_found",
  "webhook_dns_failed", "webhook_event_not_allowed", "webhook_not_found",
  "webhook_url_not_allowed", "webhook_url_unresolvable", "webhooks_unavailable",
] as const;

function additionalDescriptor(code: string): Omit<UiErrorDescriptor, "code"> {
  const retryable = RETRYABLE_CODES.has(code);
  return {
    title: "操作暂时无法完成",
    message: "本次操作未完成，当前页面没有更新。",
    suggestion: retryable ? "请稍后重试。" : "请检查当前状态后再试。",
    retryable,
    action: ACTION_BY_CODE[code] ?? (retryable ? "retry" : null),
  };
}

const CATALOG: Record<string, Omit<UiErrorDescriptor, "code">> = {
  ...Object.fromEntries(ADDITIONAL_CODES.map((code) => [code, additionalDescriptor(code)])),
  tmdb_unavailable: { title: "影视信息暂时无法加载", message: "本次影视资料没有更新，资源区和已有页面仍可查看。", suggestion: "请重新加载影视资料。", retryable: true, action: "retry" },
  rate_limited: { title: "请求过于频繁", message: "本次请求未执行，当前页面内容没有改变。", suggestion: "请稍后再试。", retryable: true, action: "retry" },
  settings_conflict: { title: "设置已在其他位置更新", message: "本次修改未保存，当前页面不是最新版本。", suggestion: "请加载最新设置后重新提交。", retryable: false, action: "reload_settings" },
  credential_rejected: { title: "凭据验证未通过", message: "新凭据未生效，原配置保持不变。", suggestion: "请检查凭据后重新验证。", retryable: false, action: "reauthenticate" },
  credential_validation_unavailable: { title: "暂时无法验证凭据", message: "本次验证未完成，原配置保持不变。", suggestion: "请稍后重新验证。", retryable: true, action: "retry" },
  credentials_unavailable: { title: "连接配置暂时不可用", message: "连接配置没有更新。", suggestion: "请稍后重新加载配置。", retryable: true, action: "retry" },
  resource_snapshot_not_found: { title: "资源列表已更新", message: "当前分页快照已失效，影视资料没有受到影响。", suggestion: "请返回第一页刷新资源。", retryable: true, action: "refresh_snapshot" },
  inspection_unsupported: { title: "当前环境无法检测资源", message: "未创建资源检测任务。", suggestion: "请查看资源检测配置。", retryable: false, action: "inspect_configuration" },
  task_not_retryable: { title: "当前任务不能直接重试", message: "原任务状态未改变。", suggestion: "请查看任务状态后再决定下一步。", retryable: false, action: "view_task" },
  task_not_cancellable: { title: "当前任务不能取消", message: "任务状态未改变，远端结果不会被伪造撤回。", suggestion: "请查看任务状态；如果远端结果不确定，请先核对后再决定下一步。", retryable: false, action: "view_task" },
  needs_auth: { title: "115 登录状态已失效", message: "任务尚未继续提交到 115。", suggestion: "请前往凭据设置重新登录。", retryable: false, action: "reauthenticate" },
  uncertain: { title: "暂时无法确认提交结果", message: "远端可能已经接受，系统不会自动重复提交。", suggestion: "请查看任务状态，确认前不要重复操作。", retryable: false, action: "view_task" },
  invalid_credentials: { title: "登录信息不正确", message: "本次登录未成功。", suggestion: "请检查后重新登录。", retryable: true, action: "retry" },
  auth_not_configured: { title: "登录服务暂时不可用", message: "本次登录未执行。", suggestion: "请稍后重试或联系管理员。", retryable: true, action: "retry" },
  resource_not_found: { title: "资源不存在", message: "本次任务未创建，资源可能已被移除。", suggestion: "请刷新资源列表后再试。", retryable: false, action: "refresh_snapshot" },
  push_kind_unsupported: { title: "当前资源类型不可推送", message: "本次任务未创建，当前环境不支持该资源类型。", suggestion: "请查看 115 推送能力配置。", retryable: false, action: "inspect_configuration" },
  workflow_not_found: { title: "关联工作流不存在", message: "本次任务未创建。", suggestion: "请刷新页面后重新操作。", retryable: false, action: "retry" },
  high_risk_approval_required: { title: "需要 Web 人工批准", message: "影响数量超过阈值，本次整理未排队。", suggestion: "请在任务中心完成人工批准后再提交。", retryable: false, action: "view_task" },
  web_approval_required: { title: "需要 Web 人工批准", message: "Agent 不能直接批准高风险计划。", suggestion: "请使用已登录的 Web 会话完成批准。", retryable: false, action: "view_task" },
  workflow_id_conflict: { title: "任务已关联其他工作流", message: "本次任务未改动现有关联。", suggestion: "请查看原工作流或创建新的搜索任务。", retryable: false, action: "view_task" },
  workflow_conflict: { title: "工作流状态发生冲突", message: "本次工作流操作未执行。", suggestion: "请刷新工作流状态后再试。", retryable: false, action: "reload_settings" },
  workflow_not_awaiting_confirmation: { title: "工作流当前不需要确认", message: "本次确认未执行。", suggestion: "请查看当前阶段状态后再操作。", retryable: false, action: "view_task" },
  workflow_not_cancellable: { title: "工作流当前不可取消", message: "本次取消未执行，运行中或远端结果不会被伪造撤回。", suggestion: "请先查看工作流阶段和远端任务状态。", retryable: false, action: "view_task" },
  library_inventory_incomplete: { title: "库存索引不完整", message: "本次库存身份确认未保存。", suggestion: "请先完成一次完整库存扫描。", retryable: false, action: "refresh_snapshot" },
  library_identity_conflict: { title: "库存身份已变化", message: "本次库存身份确认未保存。", suggestion: "请刷新库存后重新确认。", retryable: false, action: "refresh_snapshot" },
  inventory_scope_unconfigured: { title: "未配置 115 媒体库范围", message: "本次推送未提交到 115，系统还没有可核对的媒体库库存范围。", suggestion: "请前往“媒体库”配置生产根目录，验证范围并完成一次完整扫描后再重试。", retryable: false, action: "open_library" },
  inventory_index_incomplete: { title: "115 媒体库库存未完成", message: "本次推送未提交到 115，系统无法确认媒体库中是否已有该资源。", suggestion: "请前往“媒体库”完成一次完整扫描后再重试。", retryable: false, action: "open_library" },
  inventory_index_stale: { title: "115 媒体库库存已过期", message: "本次推送未提交到 115，当前库存索引不能作为去重依据。", suggestion: "请前往“媒体库”重新扫描库存后再重试。", retryable: false, action: "open_library" },
  inventory_index_unknown: { title: "115 媒体库库存状态未知", message: "本次推送未提交到 115，当前库存扫描状态不可靠。", suggestion: "请前往“媒体库”确认扫描状态并重新扫描后再重试。", retryable: false, action: "open_library" },
  inventory_exact_duplicate: { title: "资源已在 115 媒体库中", message: "本次推送未提交到 115，库存中已经存在相同资源。", suggestion: "请前往“媒体库”查看现有资源，不要重复提交。", retryable: false, action: "open_library" },
  inventory_review_required: { title: "115 媒体库存在相近资源", message: "本次推送未提交到 115，库存中存在相同媒体或待确认版本。", suggestion: "请前往“媒体库”查看库存后完成人工确认。", retryable: false, action: "open_library" },
};

export const UI_ERROR_CODES = Object.freeze(Object.keys(CATALOG));

export function describeUiError(code: string | undefined, status = 0): UiErrorDescriptor {
  const safeCode = typeof code === "string" && /^[a-z][a-z0-9_.-]{1,99}$/.test(code) ? code : "unknown_error";
  const known = CATALOG[safeCode];
  if (known) return { code: safeCode, ...known };
  const retryable = status === 408 || status === 429 || status >= 500;
  return {
    code: safeCode,
    title: "操作暂时无法完成",
    message: "本次操作未完成，当前页面没有更新。",
    suggestion: retryable ? "请稍后重试；如果问题持续，请提供诊断信息。" : "请检查输入和当前状态后再试。",
    retryable,
    action: retryable ? "retry" : null,
  };
}

export function taskErrorMessage(code: string | null): string | null {
  if (!code) return null;
  const descriptor = describeUiError(code);
  return `${descriptor.title}：${descriptor.message}${descriptor.suggestion ? ` ${descriptor.suggestion}` : ""}`;
}
