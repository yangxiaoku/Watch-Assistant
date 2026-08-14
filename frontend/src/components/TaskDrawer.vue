<script setup lang="ts">
import { AlertTriangle, Ban, CircleAlert, CircleCheck, Database, ListTodo, LoaderCircle, LogIn, RefreshCw, Timer, X } from "@lucide/vue";
import { ref, watch } from "vue";
import { ApiError, type ApiClient } from "../api";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import { describeUiError, taskErrorMessage } from "../errorCatalog";
import { taskStatusPresentation } from "../statusCatalog";
import { diagnosticCode, diagnosticReference } from "../uiSafety";
import type { TaskResponse } from "../types";

const props = defineProps<{ api: ApiClient; tasks: TaskResponse[]; open: boolean }>();
const emit = defineEmits<{
  close: [];
  navigate: [view: "library" | "settings" | "workflows"];
  updated: [task: TaskResponse];
  loaded: [tasks: TaskResponse[]];
}>();

const visibleTasks = ref<TaskResponse[]>([...props.tasks]);
const loading = ref(false);
const loadError = ref("");
const actionError = ref<{ taskId: string; message: string } | null>(null);
const actionTaskId = ref<string | null>(null);
const actionKind = ref<"retry" | "cancel" | null>(null);
const reconcilingId = ref<string | null>(null);
const reconcileError = ref<{ taskId: string; message: string } | null>(null);
const pendingCancel = ref<TaskResponse | null>(null);

watch(() => props.tasks, (tasks) => {
  visibleTasks.value = [...tasks];
}, { deep: true });

watch(() => props.open, (open) => {
  if (open) void refreshTasks();
}, { immediate: true });

function shouldOpenLibrary(code: string | null): boolean {
  return code !== null && describeUiError(code).action === "open_library";
}

function shouldOpenSettings(task: TaskResponse): boolean {
  return task.state === "needs_auth" || (task.error_code !== null && describeUiError(task.error_code).action === "reauthenticate");
}

function taskNextStep(task: TaskResponse): string {
  if (task.state_reason_zh) return task.state_reason_zh;
  return taskStatusPresentation(task.state).nextStep;
}

function canRetry(task: TaskResponse): boolean {
  return task.state === "failed" || task.state === "needs_auth";
}

function canCancel(task: TaskResponse): boolean {
  // Uncertain tasks without a remote identity cannot be reconciled; allow
  // abandoning them explicitly instead of leaving them stuck forever.
  return task.state === "queued" || (task.state === "uncertain" && !task.remote_ref);
}

function canReconcile(task: TaskResponse): boolean {
  return ["uncertain", "submitted", "downloading"].includes(task.state) && Boolean(task.remote_ref);
}

/** 各状态对应图标：queued/submitting 旋转、available 成功、submitted/downloading 计时、
 *  uncertain/needs_auth 警告、cancelled 中性、failed 错误。 */
function stateIcon(task: TaskResponse) {
  if (task.state === "queued" || task.state === "submitting") return LoaderCircle;
  if (task.state === "available") return CircleCheck;
  if (task.state === "submitted" || task.state === "downloading") return Timer;
  if (task.state === "uncertain" || task.state === "needs_auth") return AlertTriangle;
  if (task.state === "cancelled") return Ban;
  return CircleAlert;
}

function stateIconSpin(task: TaskResponse): boolean {
  return task.state === "queued" || task.state === "submitting";
}

function replaceTask(task: TaskResponse): void {
  visibleTasks.value = [task, ...visibleTasks.value.filter((item) => item.id !== task.id)].slice(0, 50);
  emit("updated", task);
}

function safeActionError(exception: unknown, fallback: string): string {
  return exception instanceof ApiError ? exception.message : fallback;
}

async function refreshTasks(): Promise<void> {
  if (loading.value || typeof props.api.listTasks !== "function") return;
  loading.value = true;
  loadError.value = "";
  try {
    const tasks = (await props.api.listTasks()).slice(0, 50);
    visibleTasks.value = tasks;
    emit("loaded", tasks);
  } catch (exception) {
    loadError.value = safeActionError(exception, "任务列表加载失败，请稍后重试。");
  } finally {
    loading.value = false;
  }
}

async function retry(task: TaskResponse): Promise<void> {
  if (!canRetry(task) || actionTaskId.value !== null) return;
  actionTaskId.value = task.id;
  actionKind.value = "retry";
  actionError.value = null;
  try {
    replaceTask(await props.api.retryTask(task.id));
  } catch (exception) {
    actionError.value = { taskId: task.id, message: safeActionError(exception, "任务重新排队失败，请刷新状态后重试。") };
  } finally {
    actionTaskId.value = null;
    actionKind.value = null;
  }
}

function requestCancel(task: TaskResponse): void {
  if (!canCancel(task) || actionTaskId.value !== null) return;
  pendingCancel.value = task;
}

function closeCancelDialog(): void {
  if (actionTaskId.value === null) pendingCancel.value = null;
}

async function confirmCancel(): Promise<void> {
  const task = pendingCancel.value;
  pendingCancel.value = null;
  if (!task || !canCancel(task) || actionTaskId.value !== null) return;
  actionTaskId.value = task.id;
  actionKind.value = "cancel";
  actionError.value = null;
  try {
    replaceTask(await props.api.cancelTask(task.id));
  } catch (exception) {
    actionError.value = { taskId: task.id, message: safeActionError(exception, "任务取消失败，请刷新状态后重试。") };
  } finally {
    actionTaskId.value = null;
    actionKind.value = null;
  }
}

async function reconcile(task: TaskResponse): Promise<void> {
  if (!canReconcile(task) || reconcilingId.value !== null) return;
  reconcilingId.value = task.id;
  reconcileError.value = null;
  try {
    const response = await props.api.reconcileTask(task.id);
    replaceTask(response.task);
  } catch (exception) {
    reconcileError.value = {
      taskId: task.id,
      message: safeActionError(exception, "只读核对暂时无法完成，请稍后重试。"),
    };
  } finally {
    reconcilingId.value = null;
  }
}
</script>

<template>
  <aside v-if="open" class="task-drawer" aria-label="推送任务">
    <header>
      <div><p class="eyebrow">任务记录</p><h2>推送任务</h2></div>
      <div class="task-drawer-header-actions">
        <button class="icon-button" type="button" title="刷新推送任务" aria-label="刷新推送任务" :disabled="loading" @click="refreshTasks"><RefreshCw :size="17" :class="{ spin: loading }" /></button>
        <button class="text-button" type="button" @click="$emit('navigate', 'workflows')"><ListTodo :size="14" />任务中心</button>
        <button class="icon-button" type="button" title="关闭推送任务" aria-label="关闭推送任务" @click="$emit('close')"><X :size="18" /></button>
      </div>
    </header>
    <div v-if="loadError" class="task-drawer-error" role="alert"><span>{{ loadError }}</span><button class="text-button" type="button" @click="refreshTasks">重试</button></div>
    <div v-if="loading && !visibleTasks.length" class="task-drawer-empty" role="status"><LoaderCircle class="spin" :size="20" />正在加载任务</div>
    <div v-else-if="!visibleTasks.length" class="empty-state">还没有推送任务</div>
    <article v-for="task in visibleTasks" :key="task.id" class="task-row">
      <div class="task-icon" :class="task.state">
        <component :is="stateIcon(task)" :class="{ spin: stateIconSpin(task) }" :size="17" />
      </div>
      <div class="task-copy">
        <div class="task-status-line"><strong>{{ task.state_zh || taskStatusPresentation(task.state).label }}</strong><span :class="['status-chip', `status-chip-${taskStatusPresentation(task.state).tone}`]">{{ taskStatusPresentation(task.state).label }}</span></div>
        <small>已加入受控推送队列</small>
        <small v-if="task.workflow_id">已关联任务流程</small>
        <small>{{ taskNextStep(task) }}</small>
        <small v-if="task.error_code">{{ taskErrorMessage(task.error_code) }}</small>
        <small v-else-if="task.error_message">任务处理未完成，详细内容已隐藏。</small>
        <div class="task-actions">
          <button v-if="canRetry(task)" class="text-button task-action" type="button" :disabled="actionTaskId !== null" @click="retry(task)"><RefreshCw :size="14" :class="{ spin: actionTaskId === task.id && actionKind === 'retry' }" />重新排队</button>
          <button v-if="canCancel(task)" class="text-button task-action" type="button" :disabled="actionTaskId !== null" @click="requestCancel(task)"><X :size="14" />取消排队</button>
          <button v-if="canReconcile(task)" class="text-button task-action" type="button" :disabled="reconcilingId !== null || actionTaskId !== null" @click="reconcile(task)"><RefreshCw :size="14" :class="{ spin: reconcilingId === task.id }" />只读核对</button>
          <button v-if="shouldOpenSettings(task)" class="text-button task-action" type="button" @click="emit('navigate', 'settings')"><LogIn :size="14" />前往登录设置</button>
          <button v-if="shouldOpenLibrary(task.error_code)" class="text-button task-action" type="button" @click="emit('navigate', 'library')"><Database :size="14" />前往媒体库配置</button>
        </div>
        <details class="task-diagnostics">
          <summary>诊断信息</summary>
          <small>任务标识：{{ diagnosticReference(task.id) }}</small>
          <small v-if="task.workflow_id">流程标识：{{ diagnosticReference(task.workflow_id) }}</small>
          <small v-if="task.error_code">错误码：{{ diagnosticCode(task.error_code) }}</small>
          <small v-if="task.remote_ref">远端引用：{{ diagnosticReference(task.remote_ref) }}</small>
        </details>
        <small v-if="actionError?.taskId === task.id" class="error-text">{{ actionError.message }}</small>
        <small v-if="reconcileError?.taskId === task.id" class="error-text">{{ reconcileError.message }}</small>
      </div>
    </article>
    <ConfirmDialog
      :open="pendingCancel !== null"
      title="确认取消排队任务"
      summary="只会取消尚未开始处理的队列项；已经提交或结果待确认的任务不会被伪造撤回。"
      :details="['取消后不会向远端提交本任务。', '如果状态已经变化，服务端会拒绝本次取消并保留原状态。']"
      confirm-label="确认取消"
      tone="danger"
      :require-acknowledgment="false"
      :busy="actionTaskId !== null"
      @cancel="closeCancelDialog"
      @confirm="confirmCancel"
    />
  </aside>
</template>
