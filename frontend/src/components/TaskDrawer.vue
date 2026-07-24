<script setup lang="ts">
import { CircleAlert, CircleCheck, LoaderCircle, X } from "@lucide/vue";
import type { TaskResponse } from "../types";

defineProps<{ tasks: TaskResponse[]; open: boolean }>();
defineEmits<{ close: [] }>();

const labels: Record<TaskResponse["state"], string> = {
  queued: "排队中",
  submitting: "提交中",
  accepted: "已推送到 115",
  needs_auth: "需要重新授权",
  failed: "提交失败",
  uncertain: "结果待确认",
};
</script>

<template>
  <aside v-if="open" class="task-drawer" aria-label="推送任务">
    <header><div><p class="eyebrow">任务中心</p><h2>推送记录</h2></div><button class="icon-button" title="关闭" @click="$emit('close')"><X :size="18" /></button></header>
    <div v-if="tasks.length === 0" class="empty-state">还没有推送任务</div>
    <article v-for="task in tasks" :key="task.id" class="task-row">
      <div class="task-icon" :class="task.state">
        <LoaderCircle v-if="task.state === 'queued' || task.state === 'submitting'" class="spin" :size="17" />
        <CircleCheck v-else-if="task.state === 'accepted'" :size="17" />
        <CircleAlert v-else :size="17" />
      </div>
      <div class="task-copy"><strong>{{ labels[task.state] }}</strong><small>{{ task.id }}</small><small v-if="task.error_message">{{ task.error_message }}</small></div>
    </article>
  </aside>
</template>
