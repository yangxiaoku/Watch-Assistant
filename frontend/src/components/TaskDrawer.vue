<script setup lang="ts">
import { CircleAlert, CircleCheck, Database, ListTodo, LoaderCircle, RefreshCw, X } from "@lucide/vue";
import { ref } from "vue";
import type { ApiClient } from "../api";
import { describeUiError, taskErrorMessage } from "../errorCatalog";
import type { TaskResponse } from "../types";

const props = defineProps<{ api: ApiClient; tasks: TaskResponse[]; open: boolean }>();
const emit = defineEmits<{
  close: [];
  navigate: [view: "library" | "workflows"];
  updated: [task: TaskResponse];
}>();
const reconcilingId = ref<string | null>(null);
const reconcileError = ref<{ taskId: string; message: string } | null>(null);

function shouldOpenLibrary(code: string | null): boolean {
  return code !== null && describeUiError(code).action === "open_library";
}

const labels: Record<TaskResponse["state"], string> = {
  queued: "排队中",
  submitting: "提交中",
  submitted: "已受理，等待文件可用",
  downloading: "下载中，等待文件可用",
  available: "文件已可用",
  needs_auth: "需要重新登录",
  failed: "提交失败",
  uncertain: "结果待确认",
  cancelled: "已取消",
};

function canReconcile(task: TaskResponse): boolean {
  return ["uncertain", "submitted", "downloading"].includes(task.state) && Boolean(task.remote_ref);
}

async function reconcile(task: TaskResponse): Promise<void> {
  if (!canReconcile(task) || reconcilingId.value !== null) return;
  reconcilingId.value = task.id;
  reconcileError.value = null;
  try {
    const response = await props.api.reconcileTask(task.id);
    emit("updated", response.task);
  } catch (exception) {
    reconcileError.value = {
      taskId: task.id,
      message: exception instanceof Error ? exception.message : "只读核对暂时无法完成",
    };
  } finally {
    reconcilingId.value = null;
  }
}
</script>

<template>
  <aside v-if="open" class="task-drawer" aria-label="推送任务">
    <header><div><p class="eyebrow">任务记录</p><h2>推送任务</h2></div><div class="task-drawer-header-actions"><button class="text-button" type="button" @click="$emit('navigate', 'workflows')"><ListTodo :size="14" />任务中心</button><button class="icon-button" type="button" title="关闭推送任务" aria-label="关闭推送任务" @click="$emit('close')"><X :size="18" /></button></div></header>
    <div v-if="tasks.length === 0" class="empty-state">还没有推送任务</div>
    <article v-for="task in tasks" :key="task.id" class="task-row">
      <div class="task-icon" :class="task.state">
        <LoaderCircle v-if="task.state === 'queued' || task.state === 'submitting' || task.state === 'submitted' || task.state === 'downloading'" class="spin" :size="17" />
        <CircleCheck v-else-if="task.state === 'available'" :size="17" />
        <CircleAlert v-else :size="17" />
      </div>
      <div class="task-copy"><strong>{{ task.state_zh || labels[task.state] }}</strong><small>任务 {{ task.id }}</small><small v-if="task.workflow_id">工作流 {{ task.workflow_id }}</small><small v-if="task.state_reason_zh">{{ task.state_reason_zh }}</small><small v-if="task.error_code">{{ taskErrorMessage(task.error_code) }}</small><small v-else-if="task.error_message">任务处理未完成，请查看状态后再试</small><button v-if="canReconcile(task)" class="text-button task-action" type="button" :disabled="reconcilingId !== null" @click="reconcile(task)"><RefreshCw :size="14" :class="{ spin: reconcilingId === task.id }" />只读核对</button><button v-if="shouldOpenLibrary(task.error_code)" class="text-button task-action" type="button" @click="emit('navigate', 'library')"><Database :size="14" />前往媒体库配置</button><small v-if="reconcileError?.taskId === task.id" class="error-text">{{ reconcileError.message }}</small></div>
    </article>
  </aside>
</template>
