import type {
  CapabilityAvailability,
  LibraryScanSummary,
  LogCategory,
  LogLevel,
  OrganizationOperationStatus,
  OrganizationPlanStatus,
  OrganizationResultItem,
  OrganizationResultStatus,
  StrmManifestItemResponse,
  StrmOperationResponse,
  TaskState,
  WorkflowStageStatus,
  WorkflowStatus,
} from "./types";
import { safeLocalizedCopy } from "./uiSafety";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

export interface StatusPresentation {
  label: string;
  tone: StatusTone;
  nextStep: string;
}

export function capabilityStatusPresentation(
  capability: Pick<CapabilityAvailability, "enabled" | "reason_code" | "reason_zh"> | null | undefined,
): StatusPresentation {
  if (capability?.enabled) {
    return { label: "可用", tone: "success", nextStep: "可以进入对应工作区继续操作。" };
  }
  if (!capability || capability.reason_code === "capability_unknown") {
    return { label: "状态待确认", tone: "warning", nextStep: "请刷新设置概览后再决定下一步。" };
  }
  return {
    label: "需配置",
    tone: "warning",
    nextStep: safeLocalizedCopy(capability.reason_zh, "请前往设置查看功能状态。"),
  };
}


const TASK_STATUS: Record<TaskState, StatusPresentation> = {
  queued: { label: "排队中", tone: "info", nextStep: "等待任务开始处理。" },
  submitting: { label: "处理中", tone: "info", nextStep: "正在向受控服务提交，请稍候。" },
  submitted: { label: "处理中", tone: "info", nextStep: "服务已受理，等待文件可用证据。" },
  downloading: { label: "处理中", tone: "info", nextStep: "正在等待文件可用证据。" },
  available: { label: "成功", tone: "success", nextStep: "已取得文件可用证据，可以继续后续阶段。" },
  needs_auth: { label: "需要处理", tone: "warning", nextStep: "请前往设置重新授权 115，再查看任务状态。" },
  failed: { label: "失败", tone: "danger", nextStep: "查看失败原因，确认状态后再按规则重试。" },
  uncertain: { label: "结果待确认", tone: "warning", nextStep: "先进行只读核对，确认前不要重复提交。" },
  cancelled: { label: "已取消", tone: "neutral", nextStep: "未开始的后续处理已停止。" },
};

const WORKFLOW_STATUS: Record<WorkflowStatus, StatusPresentation> = {
  in_progress: { label: "处理中", tone: "info", nextStep: "等待当前阶段完成。" },
  waiting_user_confirmation: { label: "等待确认", tone: "warning", nextStep: "查看影响摘要后完成所需确认。" },
  waiting_external: { label: "等待外部服务", tone: "warning", nextStep: "等待外部服务返回，不要重复提交。" },
  partial: { label: "部分完成", tone: "warning", nextStep: "查看未完成阶段，再处理可执行的下一步。" },
  completed: { label: "成功", tone: "success", nextStep: "流程已完成。" },
  cancelled: { label: "已取消", tone: "neutral", nextStep: "未开始的后续阶段已停止。" },
  failed: { label: "失败", tone: "danger", nextStep: "查看失败阶段，按阶段规则重试或重新生成计划。" },
  result_pending_confirmation: { label: "结果待确认", tone: "warning", nextStep: "先核对远端结果，确认前不要重复操作。" },
};

const STAGE_STATUS: Record<WorkflowStageStatus, string> = {
  pending: "待开始",
  running: "处理中",
  waiting_confirmation: "等待确认",
  waiting_external: "等待外部服务",
  succeeded: "成功",
  skipped: "已跳过",
  failed: "失败",
  uncertain: "结果待确认",
  cancelled: "已取消",
};

const STRM_OPERATION_STATUS: Record<StrmOperationResponse["status"], string> = {
  queued: "排队中",
  running: "处理中",
  succeeded: "成功",
  failed: "失败",
  timeout: "结果待确认",
  cancelled: "已取消",
};

const ORGANIZATION_OPERATION_STATUS: Record<OrganizationOperationStatus, string> = {
  planned: "已排队",
  organizing: "执行中",
  organized: "已完成",
  failed: "已失败",
  uncertain: "结果待确认",
  cancelled: "已取消",
};

const MANIFEST_STATUS: Record<StrmManifestItemResponse["status"], string> = {
  pending: "待生成",
  verified: "有效",
  retired: "已退休",
};

const SCAN_STATUS: Record<LibraryScanSummary["state"], string> = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function taskStatusPresentation(state: TaskState): StatusPresentation {
  return TASK_STATUS[state] ?? { label: "状态待确认", tone: "warning", nextStep: "请刷新任务状态后再决定下一步。" };
}

export function workflowStatusPresentation(status: WorkflowStatus): StatusPresentation {
  return WORKFLOW_STATUS[status] ?? { label: "状态待确认", tone: "warning", nextStep: "请刷新任务中心后再决定下一步。" };
}

export function workflowStageStatusLabel(status: WorkflowStageStatus): string {
  return STAGE_STATUS[status] ?? "状态待确认";
}

export function strmOperationStatusLabel(
  operation: Pick<StrmOperationResponse, "status" | "failed" | "skipped">,
): string {
  if (operation.status === "succeeded" && operation.failed > 0) return "部分完成";
  return STRM_OPERATION_STATUS[operation.status] ?? "状态待确认";
}

export function organizationOperationStatusLabel(
  status: OrganizationOperationStatus,
  executionSupported = true,
): string {
  if (status === "planned" && !executionSupported) return "等待执行能力（未执行）";
  return ORGANIZATION_OPERATION_STATUS[status] ?? "状态待确认";
}

export function strmOperationNextStep(
  operation: Pick<StrmOperationResponse, "status" | "failed" | "skipped">,
): string {
  if (operation.status === "timeout") return "先刷新并核对操作结果，确认前不要恢复执行。";
  if (operation.status === "failed") return "查看失败原因，确认快照仍有效后再恢复执行。";
  if (operation.status === "cancelled") return "确认没有遗留处理中操作后，再决定是否恢复执行。";
  if (operation.status === "succeeded" && operation.failed > 0) return "部分条目未完成，请查看失败统计并按需重试。";
  if (operation.status === "queued" || operation.status === "running") return "等待当前操作完成，页面会持续更新状态。";
  return "操作已完成。";
}

export function manifestStatusLabel(status: StrmManifestItemResponse["status"]): string {
  return MANIFEST_STATUS[status] ?? "状态待确认";
}

export function libraryScanStatusLabel(scan: Pick<LibraryScanSummary, "state" | "complete"> | null | undefined): string {
  if (!scan) return "未扫描";
  if (scan.state === "completed" && !scan.complete) return "未完成";
  return SCAN_STATUS[scan.state] ?? "状态待确认";
}

export interface LogLevelOption {
  value: LogLevel;
  label: string;
}

export interface LogCategoryOption {
  value: LogCategory;
  label: string;
}

export const logLevelOptions: LogLevelOption[] = [
  { value: "DEBUG", label: "调试" },
  { value: "ERROR", label: "错误" },
  { value: "WARNING", label: "警告" },
  { value: "INFO", label: "信息" },
];

export const logCategoryOptions: LogCategoryOption[] = [
  { value: "system", label: "系统" },
  { value: "security", label: "安全" },
  { value: "search", label: "搜索" },
  { value: "pansou", label: "PanSou" },
  { value: "cache", label: "缓存" },
  { value: "inspection", label: "检测" },
  { value: "p115", label: "115" },
  { value: "task", label: "任务" },
  { value: "organize", label: "整理" },
  { value: "strm", label: "STRM" },
  { value: "library", label: "媒体库" },
  { value: "agent", label: "Agent" },
  { value: "settings", label: "设置" },
  { value: "subscription", label: "订阅" },
  { value: "quality", label: "质量策略" },
  { value: "notification", label: "通知" },
];

export function logLevelLabel(level: LogLevel): string {
  return logLevelOptions.find((option) => option.value === level)?.label ?? level;
}

export function logCategoryLabel(category: LogCategory): string {
  return logCategoryOptions.find((option) => option.value === category)?.label ?? category;
}

export function workflowStatusOptions(): Array<{ value: WorkflowStatus; label: string }> {
  return (Object.entries(WORKFLOW_STATUS) as Array<[WorkflowStatus, StatusPresentation]>).map(([value, presentation]) => ({ value, label: presentation.label }));
}

export function workflowStageStatusOptions(): Array<{ value: WorkflowStageStatus; label: string }> {
  return (Object.entries(STAGE_STATUS) as Array<[WorkflowStageStatus, string]>).map(([value, label]) => ({ value, label }));
}

export const ORGANIZATION_PLAN_STATUS: Record<OrganizationPlanStatus, string> = {
  needs_review: "待确认",
  planned: "已确认（本地预览）",
  invalidated: "已失效",
  ignored: "已忽略",
};

export function organizationPlanStatusLabel(status: OrganizationPlanStatus): string {
  return ORGANIZATION_PLAN_STATUS[status] ?? "状态待确认";
}

export const ORGANIZATION_RESULT_STATUS: Record<OrganizationResultStatus, string> = {
  unknown: "尚未整理",
  success: "整理完成",
  skipped: "未执行",
  deleted: "已删除",
  replace: "已替换",
  failed: "存在失败",
};

export const ORGANIZATION_RESULT_ITEM_STATUS: Record<OrganizationResultItem["status"], string> = {
  queued: "排队中",
  organizing: "整理中",
  success: "已入库",
  failed: "失败",
  uncertain: "待确认",
  needs_review: "待确认",
  skipped: "未执行",
};

export function organizationResultStatusLabel(status: OrganizationResultStatus): string {
  return ORGANIZATION_RESULT_STATUS[status] ?? "状态待确认";
}

export function organizationResultItemStatusLabel(status: OrganizationResultItem["status"]): string {
  return ORGANIZATION_RESULT_ITEM_STATUS[status] ?? status;
}
