<script setup lang="ts">
import { AlertTriangle, Ban, Check, ChevronRight, Eye, ListChecks, LoaderCircle, Play, RefreshCw, Search, ShieldCheck, Tag } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError, focusFirstFieldError, isConflict } from "../api";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import { describeUiError } from "../errorCatalog";
import { pollUntil } from "../polling";
import { organizationOperationStatusLabel, organizationPlanStatusLabel } from "../statusCatalog";
import { diagnosticCode, diagnosticReference, safeLocalizedCopy } from "../uiSafety";
import type { OrganizationExecutionBlocker, OrganizationOperationBatchResult, OrganizationOperationResponse, OrganizationPlanStatus, OrganizationPlanSummary } from "../types";

const props = withDefaults(defineProps<{ api: ApiClient; enabled?: boolean; executionSupported?: boolean; embedded?: boolean }>(), {
  enabled: true,
  executionSupported: false,
  embedded: false,
});
// 打开设置页对应分区:organization 为「设置 → 115整理」,credentials 为「设置 → 连接配置(TMDB API Key)」
const emit = defineEmits<{ "open-settings": [section?: "organization" | "credentials"] }>();

// null = 默认视图:不传 status,由后端返回活跃计划(待确认 + 已确认);点击 tab 时才按状态过滤
const activeStatus = ref<OrganizationPlanStatus | null>(null);
const items = ref<OrganizationPlanSummary[]>([]);
const nextCursor = ref<number | null>(null);
const selected = ref<OrganizationPlanSummary | null>(null);
const operation = ref<OrganizationOperationResponse | null>(null);
const approvalWorkflowId = ref<string | null>(null);
const aliasInput = ref("");
const searchQuery = ref("");
const searchSourceIndex = ref(0);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const candidateSearchGuidance = ref(false);
const lastPlanCursor = ref<number | undefined>(undefined);
const pendingExecution = ref<
  | { kind: "single"; plan: OrganizationPlanSummary }
  | { kind: "batch"; plans: OrganizationPlanSummary[] }
  | null
>(null);

const selectedIsReviewable = computed(() => selected.value?.status === "needs_review");
// 待确认 tab 与默认视图(活跃=待确认+已确认)都允许"确认并整理当前页"批量操作
const isNeedsReviewView = computed(() => activeStatus.value === null || activeStatus.value === "needs_review");

function planBadgeLabel(plan: OrganizationPlanSummary): string {
  if (plan.status === "ignored") return "已忽略";
  if (plan.status === "invalidated") return "已失效";
  if (plan.status === "planned") return "已确认";
  return plan.executable_action_count ? "可整理" : "待识别";
}

function planBadgeClass(plan: OrganizationPlanSummary): string {
  if (plan.status === "ignored" || plan.status === "invalidated") return "organization-plan-badge-muted";
  return plan.executable_action_count ? "organization-plan-badge-ok" : "organization-plan-badge-warn";
}
const selectedFileNames = computed<string[]>(() => selected.value?.source_names ?? []);
const selectedCanEdit = computed(() => selected.value?.status === "needs_review" || selected.value?.status === "planned");
const selectedCanExecute = computed(() => selected.value?.can_execute === true);
// 批量整理只提交待确认(needs_review)计划:已确认(planned)计划已有活跃操作,
// 混入批量提交会触发 operation_plan_conflict;与 needs_review tab 行为保持一致。
const executableItems = computed(() => items.value.filter((item) => item.status === "needs_review" && item.can_execute));
// 当前选中计划是否存在活跃整理操作(排队中/执行中),存在时禁止重复发起执行
const operationActive = computed(() => {
  const status = operation.value?.status;
  return status === "planned" || status === "organizing";
});
const selectedExecutionBlockers = computed<OrganizationExecutionBlocker[]>(() => {
  const blockers = selected.value?.execution_blockers;
  if (blockers?.length) return blockers;
  return selected.value && !selected.value.can_execute
    ? [{
        kind: "status",
        code: "plan_not_executable",
        message_zh: "当前计划尚不能执行。",
        next_step_zh: "刷新计划并按提示完成复核或重新生成计划。",
      }]
    : [];
});
const executionDialogOpen = computed(() => pendingExecution.value !== null);
const executionDialogTitle = computed(() => pendingExecution.value?.kind === "batch" ? "确认批量整理" : "确认执行整理计划");
const executionDialogSummary = computed(() => pendingExecution.value?.kind === "batch"
  ? "系统将为当前页可执行计划逐项提交受控整理操作，不能执行的计划会被跳过。"
  : "系统将根据这份不可变计划提交受控整理操作，服务端仍会在写入前核对远端前置条件。"
);
const executionDialogDetails = computed(() => {
  const pending = pendingExecution.value;
  if (!pending) return [];
  if (pending.kind === "batch") {
    const executableActions = pending.plans.reduce((sum, plan) => sum + plan.executable_action_count, 0);
    return [
      `计划数量：${pending.plans.length}`,
      `预计移动：${executableActions} 项`,
      "遇到版本变化或前置条件不满足的计划时，系统会单独拒绝，不会覆盖其他计划结果。",
    ];
  }
  const plan = pending.plan;
  return [
    `计划版本：${plan.revision}`,
    `预计移动：${plan.executable_action_count} 项，共 ${plan.action_count} 个预览动作`,
    `前置条件：${plan.precondition_count} 项；执行前会再次核对计划摘要`,
  ];
});
const executionDialogConfirmLabel = computed(() => pendingExecution.value?.kind === "batch" ? "确认并提交" : "确认并排队");

let planRequestGeneration = 0;
let operationRequestGeneration = 0;
let operationPollGeneration = 0;
let batchPollGeneration = 0;
let unmounted = false;

/** 统一拼接 ApiError 的建议(suggestion),避免只展示 message 而丢失下一步指引 */
function describeError(exception: unknown, fallback: string): string {
  return exception instanceof ApiError
    ? `${exception.message}${exception.suggestion ? ` ${exception.suggestion}` : ""}`
    : fallback;
}

function normalizePlan(plan: OrganizationPlanSummary): OrganizationPlanSummary {
  return {
    ...plan,
    candidates: Array.isArray(plan.candidates) ? plan.candidates : [],
    execution_blockers: Array.isArray(plan.execution_blockers) ? plan.execution_blockers : [],
    source_names: Array.isArray(plan.source_names) ? plan.source_names : [],
  };
}

function planLabel(plan: Pick<OrganizationPlanSummary, "alias" | "plan_id">): string {
  const alias = plan.alias?.trim();
  return alias || `计划 ${diagnosticReference(plan.plan_id)}`;
}

function operationFailureMessage(code: string | null): string {
  if (code === "plan_prerequisites_changed") return "扫描快照已更新，原计划已失效。请重新扫描并生成新的整理计划后再确认。";
  if (code === "postcondition_mismatch") return "远端结果未满足计划预期，系统已停止后续写入，请先核对 115 当前状态。";
  if (code === "uncertain" || code === "outcome_unknown") return "远端结果暂时无法确认，请先核对 115 当前状态，不要重复提交。";
  if (code === "remote_write_failed") return "远端写入失败，请核对 115 目录状态后再决定是否重试。";
  if (code === "target_root_changed") return "整理目标目录已变更，当前计划需要重新确认。";
  return "整理操作未完成，请查看当前状态后再决定下一步。";
}

function invalidateOperationRequests(): void {
  operationRequestGeneration += 1;
  operationPollGeneration += 1;
  batchPollGeneration += 1; // 列表/选择变化时停止批量操作的轮询,避免结果贴到错误页面
}

function shouldPollOperation(status: OrganizationOperationResponse["status"]): boolean {
  return status === "organizing" || (status === "planned" && props.executionSupported);
}

async function loadPlanOperation(plan: OrganizationPlanSummary | null) {
  const requestGeneration = ++operationRequestGeneration;
  operation.value = null;
  const getter = props.api.organizationPlanOperation;
  if (!plan || typeof getter !== "function") return;
  try {
    const current = await getter.call(props.api, plan.plan_id);
    if (requestGeneration !== operationRequestGeneration || selected.value?.plan_id !== plan.plan_id) return;
    operation.value = current;
    if (shouldPollOperation(current.status)) void pollOperation(current.operation_id, plan.plan_id);
  } catch (exception) {
    if (requestGeneration === operationRequestGeneration && selected.value?.plan_id === plan.plan_id) {
      if (!(exception instanceof ApiError) || exception.code !== "operation_not_found") {
        operation.value = null;
        error.value = describeError(exception, "整理操作状态加载失败，请重试");
      }
    }
  }
}

async function loadPlans(cursor?: number) {
  const requestGeneration = ++planRequestGeneration;
  lastPlanCursor.value = cursor;
  if (cursor === undefined) {
    invalidateOperationRequests();
    operation.value = null;
  }
  loading.value = true;
  error.value = "";
  try {
    // 默认视图(null)不传 status,由后端返回活跃计划(待确认 + 已确认);tab 过滤时显式传 status
    const response = await props.api.organizationPlans(
      activeStatus.value === null
        ? { cursor, limit: 20 }
        : { status: activeStatus.value, cursor, limit: 20 },
    );
    if (requestGeneration !== planRequestGeneration) return;
    const pageItems = response.items.map(normalizePlan);
    const previousSelectedId = selected.value?.plan_id;
    if (cursor === undefined) {
      items.value = pageItems;
      selected.value = pageItems.find((item) => item.plan_id === previousSelectedId) ?? pageItems[0] ?? null;
    } else {
      const existingIds = new Set(items.value.map((item) => item.plan_id));
      items.value = [...items.value, ...pageItems.filter((item) => !existingIds.has(item.plan_id))];
      selected.value = selected.value ?? pageItems[0] ?? null;
    }
    nextCursor.value = response.next_cursor ?? null;
    aliasInput.value = selected.value?.alias ?? "";
    approvalWorkflowId.value = null;
    if (cursor === undefined || selected.value?.plan_id !== previousSelectedId) {
      invalidateOperationRequests();
      await loadPlanOperation(selected.value);
    }
  } catch (exception) {
    if (requestGeneration === planRequestGeneration) {
      error.value = describeError(exception, "计划列表加载失败，请稍后重试");
    }
  } finally {
    if (requestGeneration === planRequestGeneration) loading.value = false;
  }
}

function selectPlan(plan: OrganizationPlanSummary) {
  invalidateOperationRequests();
  selected.value = plan;
  approvalWorkflowId.value = null;
  aliasInput.value = plan.alias ?? "";
  searchQuery.value = "";
  searchSourceIndex.value = 0;
  notice.value = "";
  candidateSearchGuidance.value = false;
  void loadPlanOperation(plan);
}

async function refreshAfterConflict() {
  await loadPlans();
  notice.value = "计划版本已变化，已刷新当前列表";
}

async function confirmPlan() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedIsReviewable.value || !selectedCanExecute.value) return;
  await mutate("confirm", () => props.api.confirmOrganizationPlan(plan.plan_id, plan.revision));
}

function requestExecution(plan: OrganizationPlanSummary): void {
  if (busy.value || !props.executionSupported || !plan.can_execute || operationActive.value) return;
  pendingExecution.value = { kind: "single", plan: normalizePlan(plan) };
}

function requestBatchExecution(): void {
  const executable = executableItems.value;
  if (busy.value || !props.executionSupported || !isNeedsReviewView.value || !executable.length) return;
  pendingExecution.value = { kind: "batch", plans: executable.map(normalizePlan) };
}

function closeExecutionDialog(): void {
  if (!busy.value) pendingExecution.value = null;
}

async function ignorePlan() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedCanEdit.value) return;
  await mutate("ignore", () => props.api.ignoreOrganizationPlan(plan.plan_id, plan.revision));
}

async function saveAlias() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedCanEdit.value) return;
  await mutate("alias", () => props.api.aliasOrganizationPlan(plan.plan_id, aliasInput.value, plan.revision));
}

async function queueOperation(plan = selected.value) {
  if (!plan || busy.value || plan.status !== "planned" || !selectedCanExecute.value || !props.executionSupported) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const queuedOperation = approvalWorkflowId.value
      ? await props.api.queueOrganizationOperation(plan.plan_id, plan.revision, approvalWorkflowId.value)
      : await props.api.queueOrganizationOperation(plan.plan_id, plan.revision);
    await loadPlanOperation(plan);
    if (!operation.value) operation.value = queuedOperation;
    await handleQueuedOperation(queuedOperation, plan.plan_id);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      // 计划前置条件已变化等冲突(如 plan_prerequisites_changed):后端已将原计划标记失效,
      // 刷新列表使其显示"已失效",同时保留本次失败提示
      await loadPlans();
    }
    error.value = describeError(exception, "整理操作排队失败，请稍后重试");
  } finally {
    busy.value = false;
  }
}

async function confirmAndQueueOperation(plan = selected.value) {
  if (!plan || busy.value || plan.status !== "needs_review" || !selectedCanExecute.value || !props.executionSupported) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const queuedOperation = await props.api.confirmAndQueueOrganizationOperation(plan.plan_id, plan.revision);
    const updated = { ...plan, status: "planned" as const, revision: plan.revision + 1 };
    selected.value = updated;
    // 同步刷新列表行数据,避免本地仍持有旧版本,再次点击时后端报 stale_revision
    items.value = items.value.map((item) => item.plan_id === plan.plan_id ? updated : item);
    await loadPlanOperation(selected.value);
    if (!operation.value) operation.value = queuedOperation;
    await handleQueuedOperation(queuedOperation, plan.plan_id);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      await refreshAfterConflict();
    } else {
      error.value = describeError(exception, "确认并整理失败，请稍后重试");
    }
  } finally {
    busy.value = false;
  }
}

async function selectCandidate(candidate: OrganizationPlanSummary["candidates"][number]) {
  const plan = selected.value;
  if (!plan || busy.value || plan.status !== "needs_review") return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await props.api.selectOrganizationCandidate(
      plan.plan_id,
      plan.revision,
      candidate.source_object_id,
      candidate.tmdb_id,
    );
    selected.value = normalizePlan(updated);
    items.value = items.value.map((item) => item.plan_id === plan.plan_id ? normalizePlan(updated) : item);
    notice.value = updated.status === "planned"
      ? "已生成可执行计划，请确认后开始整理"
      : "已选择影片，但分类或归档路径仍需检查";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      await refreshAfterConflict();
    } else {
      error.value = describeError(exception, "选择影片失败，请稍后重试");
    }
  } finally {
    busy.value = false;
  }
}

async function requestApproval() {
  const plan = selected.value;
  if (!plan || busy.value || plan.status !== "planned" || !plan.requires_web_approval) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const workflow = await props.api.createOrganizationApprovalWorkflow(plan.plan_id, plan.revision);
    approvalWorkflowId.value = workflow.id;
    notice.value = `已创建 Web 人工审批，请前往任务中心确认（${workflow.id}）`;
  } catch (exception) {
    focusFirstFieldError(exception);
    error.value = exception instanceof ApiError ? exception.message : "创建人工审批失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}

async function searchCandidates() {
  const plan = selected.value;
  const search = props.api.searchOrganizationCandidates;
  if (
    !plan
    || busy.value
    || plan.status !== "needs_review"
    || typeof search !== "function"
    || !searchQuery.value.trim()
  ) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  candidateSearchGuidance.value = false;
  try {
    const updated = await search.call(
      props.api,
      plan.plan_id,
      plan.revision,
      searchQuery.value.trim(),
      searchSourceIndex.value,
    );
    selected.value = normalizePlan(updated);
    items.value = items.value.map((item) => item.plan_id === plan.plan_id ? normalizePlan(updated) : item);
    notice.value = updated.candidates.length
      ? "已找到候选，请选择正确影片"
      : "未找到候选，请尝试更具体的片名或年份";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      // 计划版本/状态冲突:刷新列表避免停留在过期快照上
      await refreshAfterConflict();
    } else if (exception instanceof ApiError && exception.code === "candidate_search_unavailable") {
      // 候选搜索不可用通常是 TMDB API Key 未配置或已失效:给出具体引导而不是只报错
      candidateSearchGuidance.value = true;
      error.value = "候选搜索暂不可用：通常是因为 TMDB API Key 未配置或已失效，本次搜索没有完成。";
    } else {
      error.value = describeError(exception, "候选搜索失败，请稍后重试");
    }
  } finally {
    busy.value = false;
  }
}

async function handleQueuedOperation(queuedOperation: OrganizationOperationResponse, planId: string) {
  if (queuedOperation.status === "organized") {
    notice.value = "整理已完成";
  } else if (queuedOperation.status === "failed" || queuedOperation.status === "uncertain") {
    error.value = queuedOperation.error_code
      ? describeUiError(queuedOperation.error_code, 409).message
      : "后台整理未完成，请查看操作状态";
  } else if (queuedOperation.status === "planned" && !props.executionSupported) {
    notice.value = "整理操作已保存，当前没有执行能力，尚未执行远端操作";
  } else {
    notice.value = "整理已提交，后台正在执行";
    await pollOperation(queuedOperation.operation_id, planId);
  }
}

async function confirmAndQueueCurrentPage() {
  const executable = executableItems.value;
  if (!props.executionSupported || !isNeedsReviewView.value || !executable.length || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  const skipped = items.value.length - executable.length;
  try {
    const response = await props.api.confirmAndQueueOrganizationOperations(
      executable.map((item) => ({ planId: item.plan_id, expectedRevision: item.revision })),
    );
    const accepted = response.items.filter((item) => item.status !== "rejected");
    const rejected = response.items.filter((item) => item.status === "rejected");
    await loadPlans();
    // 被拒绝的计划先给出失败提示,再进行已接受计划的轮询
    if (rejected.length) {
      const rejectedCode = rejected[0].error_code;
      error.value = `${rejected.length} 个计划未提交：${rejectedCode ? describeUiError(rejectedCode, 409).message : "当前计划状态已变化，请刷新后重试。"}`;
    }
    if (accepted.length) {
      notice.value = `已确认并提交 ${accepted.length} 个整理计划，后台正在执行${skipped ? `，跳过 ${skipped} 个未参与批量提交的计划` : ""}`;
      // 批量提交后轮询各操作直到终态,避免"提交后无反馈"
      await pollBatchOperations(accepted);
    } else if (skipped) {
      notice.value = `当前页没有新的整理操作，已跳过 ${skipped} 个未参与批量提交的计划`;
    }
  } catch (exception) {
    focusFirstFieldError(exception);
    error.value = describeError(exception, "批量确认并整理失败，请稍后重试");
  } finally {
    busy.value = false;
  }
}

/**
 * 批量整理提交后没有独立进度接口,逐个轮询已接受操作的 /organization-operations/{id}
 * 直到终态,并持续更新提示;任一操作在轮询窗口内未到终态时提示手动刷新。
 */
async function pollBatchOperations(accepted: OrganizationOperationBatchResult[]) {
  const pollable = accepted.filter((item) => item.operation_id);
  if (typeof props.api.organizationOperation !== "function" || !pollable.length) return;
  const batchGeneration = ++batchPollGeneration;
  const total = pollable.length;
  let finished = 0;
  let failed = 0;
  const isCurrent = () => !unmounted && batchGeneration === batchPollGeneration;
  const terminalResults = await Promise.all(
    pollable.map((item) => {
      const operationId = item.operation_id!;
      return pollUntil(
        async () => props.api.organizationOperation(operationId),
        {
          intervalMs: 1000,
          maxAttempts: 60,
          isCurrent,
          onResponse: (current) => {
            if (!isCurrent()) return;
            if (current.status === "organized" || current.status === "failed" || current.status === "uncertain" || current.status === "cancelled") {
              finished += 1;
              if (current.status !== "organized") failed += 1;
              notice.value = finished >= total
                ? failed > 0
                  ? `批量整理已结束：${total - failed} 个完成，${failed} 个未完成，请查看对应计划的操作状态`
                  : `批量整理已完成，共 ${total} 个计划全部完成`
                : `已确认并提交 ${accepted.length} 个整理计划，后台正在执行（已完成 ${finished}/${total}）`;
            }
          },
          isDone: (current) =>
            current.status === "organized"
            || current.status === "failed"
            || current.status === "uncertain"
            || current.status === "cancelled",
        },
      );
    }),
  );
  if (!isCurrent()) return;
  // pollUntil 对每个操作最多等 60 秒,超时或请求失败返回 null
  if (terminalResults.some((result) => result === null)) {
    notice.value = "等待超时，后台可能仍在执行，请手动刷新查看";
  }
}

async function confirmPendingExecution(): Promise<void> {
  const pending = pendingExecution.value;
  pendingExecution.value = null;
  if (!pending) return;
  if (pending.kind === "batch") {
    await confirmAndQueueCurrentPage();
    return;
  }
  if (pending.plan.status === "needs_review") await confirmAndQueueOperation(pending.plan);
  else await queueOperation(pending.plan);
}

async function pollOperation(operationId: string, planId: string) {
  if (typeof props.api.organizationOperation !== "function") return;
  const pollGeneration = ++operationPollGeneration;
  let fetchFailed = false;
  const isCurrent = () =>
    !unmounted && pollGeneration === operationPollGeneration && selected.value?.plan_id === planId;
  const terminal = await pollUntil(
    async () => {
      try {
        return await props.api.organizationOperation(operationId);
      } catch (exception) {
        fetchFailed = true;
        if (isCurrent()) {
          error.value = describeError(exception, "整理操作状态暂时无法更新，请重试");
          notice.value = "";
        }
        throw exception;
      }
    },
    {
      intervalMs: 1000,
      maxAttempts: 60,
      isCurrent,
      onResponse: (current) => {
        operation.value = current;
        if (current.status === "organized") {
          notice.value = "整理已完成";
        } else if (current.status === "failed" || current.status === "uncertain") {
          error.value = current.error_code
            ? describeUiError(current.error_code, 409).message
            : "后台整理未完成，请查看操作状态";
          notice.value = "";
        } else if (current.status === "cancelled") {
          notice.value = "整理操作已取消";
        }
      },
      isDone: (current) =>
        current.status === "organized"
        || current.status === "failed"
        || current.status === "uncertain"
        || current.status === "cancelled",
    },
  );
  // pollUntil 超时返回 null 且无请求报错时给出提示,而不是静默结束
  if (terminal === null && isCurrent() && !fetchFailed) {
    notice.value = "等待超时，后台可能仍在执行，请手动刷新查看";
  }
}

async function mutate(action: "confirm" | "ignore" | "alias", operation: () => Promise<OrganizationPlanSummary>) {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await operation();
    selected.value = normalizePlan(updated);
    aliasInput.value = updated.alias ?? "";
    await loadPlanOperation(updated);
    items.value = items.value.map((item) => item.plan_id === updated.plan_id ? normalizePlan(updated) : item);
    notice.value = action === "confirm" ? "已确认本地计划，未执行远端写操作" : action === "ignore" ? "已忽略本地计划" : "本地别名已保存";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      await refreshAfterConflict();
    } else {
      error.value = describeError(exception, "操作失败，请稍后重试");
    }
  } finally {
    busy.value = false;
  }
}

async function changeStatus(status: OrganizationPlanStatus | null) {
  if (busy.value || activeStatus.value === status) return;
  activeStatus.value = status;
  await loadPlans();
}

onMounted(() => {
  if (props.enabled) void loadPlans();
});

onBeforeUnmount(() => {
  // 组件卸载后停止仍在进行的操作轮询,避免继续更新已卸载视图的状态
  unmounted = true;
  batchPollGeneration += 1;
});
</script>

<template>
  <section v-if="enabled" class="organization-workbench">
    <div v-if="!embedded" class="organization-heading">
      <div>
        <h1>整理</h1>
        <p>{{ executionSupported ? "放入源目录的影片会自动识别并整理到归档目录。" : "当前未开启整理执行能力，只能确认本地计划。" }}</p>
      </div>
      <button class="icon-button" type="button" title="刷新" aria-label="刷新" :disabled="loading || busy" @click="loadPlans()"><RefreshCw :size="17" :class="{ spin: loading }" /></button>
    </div>

    <p v-if="error" class="error-strip" role="alert"><Ban :size="16" /><span>{{ error }}</span><button class="text-button" type="button" @click="loadPlans(lastPlanCursor)">重试</button></p>
    <p v-if="notice" class="success-strip"><Check :size="16" />{{ notice }}</p>

    <div class="organization-tabs" role="tablist" aria-label="计划列表">
      <button type="button" :class="{ active: activeStatus === null }" @click="changeStatus(null)">待处理</button>
      <button type="button" :class="{ active: activeStatus === 'ignored' }" @click="changeStatus('ignored')">已忽略</button>
    </div>

    <p v-if="!executionSupported && executableItems.length" class="organization-execution-guidance" role="note">
      <AlertTriangle :size="16" />
      <span>整理执行能力未开启（需要 115 写契约验证），当前只能确认本地计划，不会执行远端移动。可前往设置查看。</span>
      <button class="text-button" type="button" @click="emit('open-settings', 'organization')">前往设置</button>
    </p>

    <div v-if="loading && !items.length" class="organization-empty"><LoaderCircle class="spin" :size="22" /><span>正在加载</span></div>
    <div v-else-if="error && !items.length" class="organization-empty" role="alert"><Ban :size="22" /><strong>加载失败</strong><span>请重试。</span></div>
    <div v-else-if="!items.length" class="organization-empty"><Eye :size="22" /><strong>暂无待处理</strong><span>源目录没有新的影片。</span></div>
    <div v-else class="organization-layout">
      <div class="organization-list" aria-label="计划列表">
        <p v-if="loading" class="organization-list-loading" role="status"><LoaderCircle class="spin" :size="16" />正在加载下一页</p>
        <button v-if="executionSupported && isNeedsReviewView && executableItems.length" class="primary-button organization-batch-action" type="button" :disabled="loading || busy" @click="requestBatchExecution"><ListChecks :size="16" />整理全部（{{ executableItems.length }}）</button>
        <button v-for="plan in items" :key="plan.plan_id" type="button" class="organization-plan-row" :class="{ active: selected?.plan_id === plan.plan_id }" @click="selectPlan(plan)">
          <span class="organization-plan-row-main">
            <span class="organization-plan-row-sub">
              <span class="organization-plan-badge" :class="planBadgeClass(plan)">{{ planBadgeLabel(plan) }}</span>
              <small v-if="plan.source_names && plan.source_names.length > 1">{{ plan.source_names.length }} 个文件</small>
            </span>
            <span v-if="plan.source_names && plan.source_names.length" class="organization-plan-row-files">
              <span v-for="(name, index) in plan.source_names" :key="index" class="organization-plan-row-files-name" :title="safeLocalizedCopy(name, '')">{{ safeLocalizedCopy(name, "（无文件）") }}</span>
            </span>
            <span v-else class="organization-plan-row-files-name">（无文件）</span>
          </span>
          <ChevronRight :size="16" />
        </button>
        <button v-if="nextCursor !== null" class="secondary-button organization-more" type="button" :disabled="loading || busy" @click="loadPlans(nextCursor!)">加载更多</button>
      </div>

      <article v-if="selected" class="organization-preview">
        <div class="organization-preview-heading">
          <div><h2>{{ selected.source_names?.length ? safeLocalizedCopy(selected.source_names[0], "") : "整理计划" }}</h2><small v-if="selected.source_names && selected.source_names.length > 1">共 {{ selected.source_names.length }} 个文件</small></div>
        </div>
        <dl class="organization-facts">
          <div><dt>计划标识</dt><dd>{{ selected.plan_id }}</dd></div>
          <div><dt>版本</dt><dd>{{ selected.revision }}</dd></div>
          <div><dt>来源条目</dt><dd>{{ selected.source_count }}</dd></div>
          <div><dt>预览动作</dt><dd>{{ selected.action_count }}</dd></div>
          <div><dt>前置条件</dt><dd>{{ selected.precondition_count }}</dd></div>
        </dl>
        <p v-if="selected.requires_web_approval" class="organization-safe-note"><ShieldCheck :size="15" />影响动作超过 {{ selected.high_risk_action_threshold }} 条，提交远端整理前必须完成 Web 人工审批。</p>
        <p class="organization-safe-note">预览只显示本地摘要。</p>

        <section v-if="selected.executable_action_count" class="organization-file-group organization-file-group-ready">
          <strong>可自动整理（{{ selected.executable_action_count }}）</strong>
          <p class="organization-file-group-hint">将移动到归档目录并规范命名。</p>
        </section>
        <section v-if="selected.review_action_count || selected.candidates.length" class="organization-file-group organization-file-group-review">
          <strong>待识别（{{ selected.review_action_count }}）</strong>
          <p class="organization-file-group-hint">无法自动识别片名，请为每个文件选择影片。</p>
          <ul class="organization-file-list">
            <li v-for="(name, index) in selectedFileNames" :key="index" :title="safeLocalizedCopy(name, '（名称未提供）')">{{ safeLocalizedCopy(name, "（名称未提供）") }}</li>
          </ul>
          <button v-for="candidate in selected.candidates" :key="`${candidate.source_object_id}-${candidate.tmdb_id}`" type="button" class="organization-candidate" :disabled="busy" @click="selectCandidate(candidate)">
            <span>{{ candidate.title }}</span><small>{{ candidate.media_type === 'tv' ? '剧集' : '电影' }}<template v-if="candidate.release_year"> · {{ candidate.release_year }}</template></small>
          </button>
        </section>
        <form v-if="selected.status === 'needs_review'" class="organization-candidate-search" @submit.prevent="searchCandidates">
          <div class="organization-candidate-search-controls"><input id="organization-candidate-query" v-model="searchQuery" type="search" maxlength="200" placeholder="搜索片名…" autocomplete="off" /><button class="secondary-button" type="submit" :disabled="busy || !searchQuery.trim()"><LoaderCircle v-if="busy" class="spin" :size="15" /><Search v-else :size="15" />搜索</button></div>
        </form>
        <p v-if="candidateSearchGuidance" class="organization-candidate-search-guidance" role="alert">
          <AlertTriangle :size="16" />
          <span>候选搜索需要 TMDB API Key，请前往「设置 → 连接配置」检查。</span>
          <button class="text-button" type="button" @click="emit('open-settings', 'credentials')">前往设置</button>
        </p>

        <div v-if="operation" class="organization-operation-status" :class="{ failed: operation.status === 'failed', uncertain: operation.status === 'uncertain' }">
          <strong>整理操作：{{ organizationOperationStatusLabel(operation.status, executionSupported) }}</strong>
          <span v-if="operation.status === 'planned' && !executionSupported">当前没有可用整理 worker，本次操作尚未执行；恢复执行能力后请重新确认。</span>
          <span v-if="operation.status === 'failed'">{{ operationFailureMessage(operation.error_code) }}</span>
          <span v-else-if="operation.status === 'uncertain'">{{ operationFailureMessage(operation.error_code) }}</span>
          <span v-else-if="operation.status === 'organizing'">后台正在执行。</span>
        </div>

        <div v-if="selectedCanEdit || (selected.status === 'planned' && executionSupported)" class="organization-actions">
          <button v-if="selectedCanExecute && executionSupported" class="primary-button" type="button" :disabled="busy || operationActive" @click="requestExecution(selected)"><Play :size="16" />开始整理</button>
          <button v-else-if="selectedCanExecute" class="primary-button" type="button" :disabled="busy" @click="confirmPlan"><Check :size="16" />确认计划</button>
          <button v-if="selected.status === 'planned' && executionSupported && selected.requires_web_approval" class="secondary-button" type="button" :disabled="busy" @click="requestApproval"><ShieldCheck :size="16" />申请 Web 人工审批</button>
        </div>
        <details class="organization-diagnostics">
          <summary>更多操作</summary>
          <button class="text-button" type="button" :disabled="busy" @click="ignorePlan"><Ban :size="14" />忽略这批文件</button>
          <small>版本 {{ selected.revision }} · 计划 {{ diagnosticReference(selected.plan_id) }}</small>
        </details>
      </article>
    </div>
    <ConfirmDialog :open="executionDialogOpen" :title="executionDialogTitle" :summary="executionDialogSummary" :details="executionDialogDetails" :confirm-label="executionDialogConfirmLabel" :require-acknowledgment="false" :busy="busy" @cancel="closeExecutionDialog" @confirm="confirmPendingExecution" />
  </section>
</template>
