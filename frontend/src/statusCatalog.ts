import type {
  LibraryScanSummary,
  StrmManifestItemResponse,
  StrmOperationResponse,
  TaskState,
  WorkflowStageStatus,
  WorkflowStatus,
} from "./types";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

export interface StatusPresentation {
  label: string;
  tone: StatusTone;
  nextStep: string;
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
  partial: { label: "部分成功", tone: "warning", nextStep: "查看未完成阶段，再处理可执行的下一步。" },
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
  if (operation.status === "succeeded" && operation.failed > 0) return "部分成功";
  return STRM_OPERATION_STATUS[operation.status] ?? "状态待确认";
}

export function strmOperationNextStep(
  operation: Pick<StrmOperationResponse, "status" | "failed" | "skipped">,
): string {
  if (operation.status === "timeout") return "先刷新并核对操作结果，确认前不要恢复执行。";
  if (operation.status === "failed") return "查看失败原因，确认快照仍有效后再恢复执行。";
  if (operation.status === "cancelled") return "确认没有遗留处理中操作后，再决定是否恢复执行。";
  if (operation.status === "succeeded" && operation.failed > 0) return "部分条目未完成，请查看统计并按需重试。";
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
