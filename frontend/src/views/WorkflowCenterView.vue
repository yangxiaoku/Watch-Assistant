<script setup lang="ts">
import { AlertTriangle, Ban, CheckCircle2, Clock3, LoaderCircle, PanelRight, RefreshCw, X, XCircle } from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import { formatTimestamp } from "../format";
import { workflowStageStatusLabel, workflowStageStatusOptions, workflowStatusOptions, workflowStatusPresentation } from "../statusCatalog";
import { diagnosticCode, diagnosticReference } from "../uiSafety";
import type { WorkflowResponse, WorkflowStageResponse, WorkflowStatus } from "../types";

const props = defineProps<{ api: ApiClient }>();
defineEmits<{ "open-push-tasks": [] }>();

const statusFilter = ref<WorkflowStatus | "">("");
const stageFilter = ref<WorkflowStageResponse["stage"] | "">("");
const stageStatusFilter = ref<WorkflowStageResponse["status"] | "">("");
const subscriptionFilter = ref("");
const items = ref<WorkflowResponse[]>([]);
const selected = ref<WorkflowResponse | null>(null);
const loading = ref(false);
const error = ref("");
const detailLoading = ref(false);
const detailError = ref("");
const actionLoading = ref(false);
const actionError = ref("");
const page = ref(1);
const pageSize = 20;
const total = ref(0);
const pendingAction = ref<"approve" | "reject" | "cancel" | null>(null);
const batchBusy = ref(false);
const batchNotice = ref("");
const batchError = ref("");
const pendingBatchCancel = ref(false);
let workflowListRequestId = 0;
let workflowDetailRequestId = 0;

const stageLabels: Record<WorkflowStageResponse["stage"], string> = {
  discovery: "发现资源",
  inspection: "内容检测",
  approval: "人工确认",
  push: "推送 115",
  availability: "等待文件可用",
  organization: "自动整理",
  strm: "更新 STRM",
};

const statusOptions = computed(() => [
  { value: "", label: "全部状态" },
  ...workflowStatusOptions(),
]);
const stageOptions = computed(() => [
  { value: "", label: "全部阶段" },
  ...Object.entries(stageLabels).map(([value, label]) => ({ value, label })),
]);
const stageStatusOptions = computed(() => [
  { value: "", label: "全部阶段状态" },
  ...workflowStageStatusOptions(),
]);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));
const confirmationTitle = computed(() => {
  if (pendingAction.value === "approve") return "确认继续工作流";
  if (pendingAction.value === "reject") return "拒绝并停止工作流";
  return "取消工作流";
});
const confirmationSummary = computed(() => {
  if (pendingAction.value === "approve") return "服务端将按当前阶段顺序继续尚未开始的处理。";
  if (pendingAction.value === "reject") return "本次确认会停止等待人工确认的后续阶段，不会伪造已经发生的远端结果。";
  return "只会停止尚未开始的阶段；运行中或结果待确认的远端操作不会被伪造撤回。";
});
const confirmationDetails = computed(() => {
  const workflow = selected.value;
  if (!workflow) return [];
  const pendingStages = workflow.stages.filter(isCancellableStage);
  return [
    `当前状态：${statusLabel(workflow.status)}`,
    pendingStages.length ? `待处理阶段：${pendingStages.map((stage) => stageLabel(stage.stage)).join("、")}` : "当前没有可继续的待处理阶段",
    "关联任务：当前工作流",
  ];
});
const confirmationLabel = computed(() => pendingAction.value === "approve" ? "确认继续" : pendingAction.value === "reject" ? "拒绝并停止" : "确认取消");
const cancellableStageStatuses: WorkflowStageResponse["status"][] = ["pending", "waiting_confirmation", "waiting_external"];

function statusLabel(status: WorkflowStatus): string { return workflowStatusPresentation(status).label; }
function stageLabel(stage: WorkflowStageResponse["stage"]): string { return stageLabels[stage] ?? "未命名阶段"; }
function childTypeLabel(childType: string): string {
  return (
    {
      resource: "资源",
      resource_search: "资源搜索",
      task: "推送任务",
      inspection_batch: "检测批次",
      organization_operation: "整理操作",
      strm_operation: "STRM 操作",
      strm_dirty_generation: "STRM 目录更新",
    }[childType] ?? childType
  );
}
function statusIcon(status: WorkflowStageResponse["status"]) {
  if (status === "succeeded") return CheckCircle2;
  if (status === "failed" || status === "uncertain") return XCircle;
  if (status === "running" || status === "waiting_external" || status === "waiting_confirmation") return LoaderCircle;
  return Clock3;
}
function stageClass(status: WorkflowStageResponse["status"]): string { return `workflow-stage-${status}`; }
function nextStep(workflow: WorkflowResponse): string {
  return workflowStatusPresentation(workflow.status).nextStep;
}
function isCancellableStage(stage: WorkflowStageResponse): boolean {
  return cancellableStageStatuses.includes(stage.status);
}
const approvalWaiting = computed(() => selected.value?.stages.some((stage) => stage.stage === "approval" && stage.status === "waiting_confirmation") ?? false);
const workflowCanCancel = computed(() => {
  const workflow = selected.value;
  if (!workflow || ["completed", "cancelled", "failed"].includes(workflow.status)) return false;
  return workflow.stages.some(isCancellableStage);
});

async function loadWorkflows(targetPage = page.value) {
  const requestId = ++workflowListRequestId;
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.workflows({
      status: statusFilter.value || undefined,
      stage: stageFilter.value || undefined,
      stageStatus: stageStatusFilter.value || undefined,
      subscriptionId: subscriptionFilter.value.trim() || undefined,
      page: targetPage,
      pageSize,
    });
    if (requestId !== workflowListRequestId) return;
    items.value = response.items;
    page.value = Math.max(1, response.page || targetPage);
    total.value = Math.max(0, response.total || 0);
    if (selected.value) {
      const refreshed = response.items.find((item) => item.id === selected.value?.id);
      if (refreshed) selected.value = refreshed;
    }
  } catch (exception) {
    if (requestId !== workflowListRequestId) return;
    error.value = exception instanceof ApiError ? exception.message : "任务中心暂时无法加载";
  } finally {
    if (requestId === workflowListRequestId) loading.value = false;
  }
}

function changeFilter(): void {
  page.value = 1;
  void loadWorkflows(1);
}

function goToPage(targetPage: number): void {
  if (loading.value) return;
  const safePage = Math.max(1, Math.min(totalPages.value, Math.trunc(targetPage)));
  if (safePage === page.value) return;
  void loadWorkflows(safePage);
}

async function selectWorkflow(workflow: WorkflowResponse) {
  const requestId = ++workflowDetailRequestId;
  selected.value = workflow;
  detailLoading.value = true;
  detailError.value = "";
  try {
    const response = await props.api.workflow(workflow.id);
    if (requestId !== workflowDetailRequestId) return;
    selected.value = response;
  } catch (exception) {
    if (requestId !== workflowDetailRequestId) return;
    detailError.value = exception instanceof ApiError ? exception.message : "任务详情暂时无法加载";
  } finally {
    if (requestId === workflowDetailRequestId) detailLoading.value = false;
  }
}

function clearSelection() {
  workflowDetailRequestId += 1;
  selected.value = null;
  detailLoading.value = false;
  detailError.value = "";
  actionError.value = "";
}

function requestApproval(decision: "approve" | "reject"): void {
  if (!selected.value || actionLoading.value) return;
  pendingAction.value = decision;
}

function requestCancel(): void {
  if (!selected.value || actionLoading.value) return;
  pendingAction.value = "cancel";
}

function closeConfirmation(): void {
  if (!actionLoading.value) pendingAction.value = null;
}

async function decideApproval(decision: "approve" | "reject") {
  if (!selected.value || actionLoading.value) return;
  actionLoading.value = true;
  actionError.value = "";
  try {
    selected.value = await props.api.decideWorkflowApproval(selected.value.id, decision);
    await loadWorkflows();
  } catch (exception) {
    actionError.value = exception instanceof ApiError ? exception.message : "人工确认暂时无法提交";
  } finally {
    actionLoading.value = false;
  }
}

async function cancelSelectedWorkflow() {
  if (!selected.value || actionLoading.value) return;
  actionLoading.value = true;
  actionError.value = "";
  try {
    selected.value = await props.api.cancelWorkflow(selected.value.id);
    await loadWorkflows();
  } catch (exception) {
    actionError.value = exception instanceof ApiError ? exception.message : "取消工作流暂时无法提交";
  } finally {
    actionLoading.value = false;
  }
}

async function confirmPendingAction(): Promise<void> {
  const action = pendingAction.value;
  pendingAction.value = null;
  if (action === "approve" || action === "reject") await decideApproval(action);
  if (action === "cancel") await cancelSelectedWorkflow();
}

async function runBatchRetry(): Promise<void> {
  if (batchBusy.value) return;
  batchBusy.value = true;
  batchError.value = "";
  batchNotice.value = "";
  try {
    const result = await props.api.retryTasksBatch();
    batchNotice.value = result.failed.length
      ? `已重试 ${result.retried} 个失败任务，${result.failed.length} 个暂不可重试。`
      : `已重试 ${result.retried} 个失败任务。`;
    await loadWorkflows();
  } catch (exception) {
    batchError.value = exception instanceof ApiError ? exception.message : "批量重试暂时无法提交";
  } finally {
    batchBusy.value = false;
  }
}

async function confirmBatchCancel(): Promise<void> {
  if (batchBusy.value) return;
  pendingBatchCancel.value = false;
  batchBusy.value = true;
  batchError.value = "";
  batchNotice.value = "";
  try {
    const result = await props.api.cancelWorkflowsBatch();
    batchNotice.value = result.failed.length
      ? `已取消 ${result.cancelled} 个工作流，${result.failed.length} 个无待停止阶段。`
      : `已取消 ${result.cancelled} 个工作流。`;
    await loadWorkflows();
  } catch (exception) {
    batchError.value = exception instanceof ApiError ? exception.message : "批量取消暂时无法提交";
  } finally {
    batchBusy.value = false;
  }
}

onMounted(() => { void loadWorkflows(); });
</script>

<template>
  <section class="workflow-center" aria-labelledby="workflow-title">
    <header class="library-heading workflow-heading">
      <div><p class="eyebrow">任务状态与阶段</p><h1 id="workflow-title">任务中心</h1><p>统一查看搜索、检测、推送、整理和 STRM 的关联进度。</p></div>
      <button class="secondary-button workflow-push-link" type="button" @click="$emit('open-push-tasks')"><PanelRight :size="16" />查看推送任务</button>
      <button class="secondary-button" type="button" :disabled="batchBusy" @click="runBatchRetry"><RefreshCw :size="15" :class="{ spin: batchBusy }" />重试失败任务</button>
      <button class="secondary-button" type="button" :disabled="batchBusy" @click="pendingBatchCancel = true"><Ban :size="15" />清理卡住工作流</button>
      <div class="workflow-toolbar"><label for="workflow-status">状态</label><select id="workflow-status" v-model="statusFilter" @change="changeFilter"><option v-for="option in statusOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><label for="workflow-stage">阶段</label><select id="workflow-stage" v-model="stageFilter" @change="changeFilter"><option v-for="option in stageOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><label for="workflow-stage-status">阶段状态</label><select id="workflow-stage-status" v-model="stageStatusFilter" @change="changeFilter"><option v-for="option in stageStatusOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><label for="workflow-subscription">订阅</label><input id="workflow-subscription" v-model="subscriptionFilter" type="search" placeholder="输入订阅筛选" @change="changeFilter" /><button class="icon-button" type="button" title="刷新任务中心" aria-label="刷新任务中心" :disabled="loading" @click="loadWorkflows(page)"><RefreshCw :size="17" :class="{ spin: loading }" /></button></div>
    </header>
    <div v-if="error" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ error }}</span><button class="text-button" type="button" @click="loadWorkflows">重试</button></div>
    <div v-if="loading && !items.length && !error" class="workflow-empty" role="status"><LoaderCircle class="spin" :size="22" />正在加载任务中心</div>
    <div v-else-if="!items.length && !error" class="workflow-empty">当前没有关联任务</div>
    <div v-else-if="items.length" class="workflow-layout">
      <div class="workflow-list" aria-label="关联任务列表">
        <button v-for="workflow in items" :key="workflow.id" type="button" class="workflow-row" :class="{ active: selected?.id === workflow.id }" @click="selectWorkflow(workflow)"><span class="workflow-row-title">{{ workflow.media_type === 'tv' ? '电视剧' : '电影' }}</span><strong :class="`status-text-${workflowStatusPresentation(workflow.status).tone}`">{{ statusLabel(workflow.status) }}</strong><small>更新于 {{ formatTimestamp(workflow.updated_at) }}</small><small class="workflow-row-next-step">{{ nextStep(workflow) }}</small></button>
      </div>
      <article v-if="selected" class="workflow-detail" aria-live="polite">
        <header><div><p class="eyebrow">关联任务详情</p><h2>{{ selected.media_type === 'tv' ? '电视剧' : '电影' }} · {{ statusLabel(selected.status) }}</h2><p>{{ selected.state_reason_zh || '服务端正在根据阶段状态计算结果。' }}</p><div class="workflow-next-step"><strong>下一步</strong><span>{{ nextStep(selected) }}</span></div><details v-if="selected.state_reason" class="diagnostic-details"><summary>诊断信息</summary><small>状态标识：{{ diagnosticCode(selected.state_reason) }}</small></details></div><button class="icon-button" type="button" title="关闭详情" aria-label="关闭详情" @click="clearSelection"><X :size="18" /></button></header>
        <p v-if="detailLoading" class="workflow-detail-loading" role="status"><LoaderCircle class="spin" :size="16" />正在刷新任务详情</p>
        <p v-if="detailError" class="error-text" role="alert">{{ detailError }}</p>
        <p v-if="actionError" class="error-text" role="alert">{{ actionError }}</p>
        <div v-if="approvalWaiting || workflowCanCancel" class="workflow-actions">
          <button v-if="approvalWaiting" class="text-button" type="button" :disabled="actionLoading" @click="requestApproval('approve')"><CheckCircle2 :size="15" />确认继续</button>
          <button v-if="approvalWaiting" class="text-button" type="button" :disabled="actionLoading" @click="requestApproval('reject')"><XCircle :size="15" />拒绝并停止</button>
          <button v-if="workflowCanCancel" class="text-button" type="button" :disabled="actionLoading" @click="requestCancel"><Ban :size="15" />取消工作流</button>
        </div>
        <dl class="workflow-identifiers"><div><dt>媒体类型</dt><dd>{{ selected.media_type === 'tv' ? '电视剧' : '电影' }}</dd></div><div><dt>创建时间</dt><dd>{{ formatTimestamp(selected.created_at) }}</dd></div><div><dt>更新时间</dt><dd>{{ formatTimestamp(selected.updated_at) }}</dd></div></dl>
        <details class="workflow-diagnostics"><summary>查看关联标识</summary><small>工作流标识：{{ diagnosticReference(selected.id) }}</small><small>关联标识：{{ diagnosticReference(selected.correlation_id) }}</small></details>
        <ol class="workflow-timeline"><li v-for="stage in selected.stages" :key="stage.id" class="workflow-stage" :class="stageClass(stage.status)"><component :is="statusIcon(stage.status)" :size="17" :class="{ spin: stage.status === 'running' }" /><div><strong>{{ stageLabel(stage.stage) }}</strong><span>{{ stage.status_zh || workflowStageStatusLabel(stage.status) }}</span><small v-if="stage.reason_zh">{{ stage.reason_zh }}</small><details v-if="stage.reason || stage.error_code" class="diagnostic-details"><summary>诊断信息</summary><small v-if="stage.reason">状态标识：{{ diagnosticCode(stage.reason) }}</small><small v-if="stage.error_code">错误码：{{ diagnosticCode(stage.error_code) }}</small></details><small v-if="stage.child_type" class="child-subtask" title="关联子任务">关联子任务：{{ childTypeLabel(stage.child_type) }}{{ stage.child_id ? `（${stage.child_id}）` : "" }}</small><small v-if="stage.updated_at">更新于 {{ formatTimestamp(stage.updated_at) }}</small></div></li></ol>
      </article>
    </div>
    <nav v-if="!loading && totalPages > 1" class="workflow-pagination" aria-label="任务中心分页"><span>第 {{ page }} / {{ totalPages }} 页，共 {{ total }} 个工作流</span><div><button class="icon-button" type="button" aria-label="上一页" :disabled="page <= 1" @click="goToPage(page - 1)">上一页</button><button class="icon-button" type="button" aria-label="下一页" :disabled="page >= totalPages" @click="goToPage(page + 1)">下一页</button></div></nav>
    <p v-if="batchNotice" class="workflow-batch-notice" role="status">{{ batchNotice }}</p>
    <p v-if="batchError" class="error-text" role="alert">{{ batchError }}</p>
    <ConfirmDialog :open="pendingAction !== null" :title="confirmationTitle" :summary="confirmationSummary" :details="confirmationDetails" :confirm-label="confirmationLabel" :tone="pendingAction === 'reject' || pendingAction === 'cancel' ? 'danger' : 'primary'" :require-acknowledgment="false" :busy="actionLoading" @cancel="closeConfirmation" @confirm="confirmPendingAction" />
    <ConfirmDialog :open="pendingBatchCancel" title="批量取消工作流" summary="只会停止尚未开始或等待中的阶段；运行中或结果待确认的远端操作不会被伪造撤回。" confirm-label="确认批量取消" tone="danger" :require-acknowledgment="false" :busy="batchBusy" @cancel="pendingBatchCancel = false" @confirm="confirmBatchCancel" />
  </section>
</template>
