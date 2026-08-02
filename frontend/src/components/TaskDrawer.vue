<script setup lang="ts">
import { CircleAlert, CircleCheck, Database, ListTodo, LoaderCircle, X } from "@lucide/vue";
import { describeUiError, taskErrorMessage } from "../errorCatalog";
import type { TaskResponse } from "../types";

defineProps<{ tasks: TaskResponse[]; open: boolean }>();
defineEmits<{ close: []; navigate: [view: "library" | "workflows"] }>();

function shouldOpenLibrary(code: string | null): boolean {
  return code !== null && describeUiError(code).action === "open_library";
}

const labels: Record<TaskResponse["state"], string> = {
  queued: "排队中",
  submitting: "提交中",
  accepted: "已推送到 115",
  needs_auth: "需要重新登录",
  failed: "提交失败",
  uncertain: "结果待确认",
};
</script>

<template>
  <aside v-if="open" class="task-drawer" aria-label="推送任务">
    <header><div><p class="eyebrow">任务记录</p><h2>推送任务</h2></div><div class="task-drawer-header-actions"><button class="text-button" type="button" @click="$emit('navigate', 'workflows')"><ListTodo :size="14" />任务中心</button><button class="icon-button" type="button" title="关闭推送任务" aria-label="关闭推送任务" @click="$emit('close')"><X :size="18" /></button></div></header>
    <div v-if="tasks.length === 0" class="empty-state">还没有推送任务</div>
    <article v-for="task in tasks" :key="task.id" class="task-row">
      <div class="task-icon" :class="task.state">
        <LoaderCircle v-if="task.state === 'queued' || task.state === 'submitting'" class="spin" :size="17" />
        <CircleCheck v-else-if="task.state === 'accepted'" :size="17" />
        <CircleAlert v-else :size="17" />
      </div>
      <div class="task-copy"><strong>{{ labels[task.state] }}</strong><small>任务 {{ task.id }}</small><small v-if="task.workflow_id">工作流 {{ task.workflow_id }}</small><small v-if="task.error_code">{{ taskErrorMessage(task.error_code) }}</small><small v-else-if="task.error_message">任务处理未完成，请查看状态后再试</small><button v-if="shouldOpenLibrary(task.error_code)" class="text-button task-action" type="button" @click="$emit('navigate', 'library')"><Database :size="14" />前往媒体库配置</button></div>
    </article>
  </aside>
</template>
