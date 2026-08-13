<script setup lang="ts">
import { AlertTriangle, CircleAlert, CircleCheck, Info, X } from "@lucide/vue";
import { onBeforeUnmount, watch } from "vue";
import { DEFAULT_DURATION, useFeedback, type ToastItem } from "../composables/useFeedback";

const feedback = useFeedback();
const timers = new Map<number, ReturnType<typeof setTimeout>>();

const LEVEL_ICON = {
  success: CircleCheck,
  info: Info,
  warning: AlertTriangle,
  error: CircleAlert,
} as const;

function effectiveDuration(item: ToastItem): number {
  return item.duration ?? DEFAULT_DURATION[item.level];
}

function schedule(item: ToastItem): void {
  const duration = effectiveDuration(item);
  if (!Number.isFinite(duration)) return;
  timers.set(
    item.id,
    setTimeout(() => {
      timers.delete(item.id);
      feedback.dismiss(item.id);
    }, duration),
  );
}

watch(
  feedback.toasts,
  (items, previous) => {
    const previousIds = new Set((previous ?? []).map((item) => item.id));
    const added = items.filter((item) => !previousIds.has(item.id));
    added.forEach(schedule);
    for (const id of [...timers.keys()]) {
      if (!items.some((item) => item.id === id)) {
        clearTimeout(timers.get(id));
        timers.delete(id);
      }
    }
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  for (const timer of timers.values()) clearTimeout(timer);
  timers.clear();
});
</script>

<template>
  <div v-if="feedback.toasts.value.length" class="toast-stack" role="status" aria-live="polite">
    <article v-for="item in feedback.toasts.value" :key="item.id" class="toast" :class="`toast-${item.level}`" role="alert">
      <component :is="LEVEL_ICON[item.level]" :size="17" class="toast-icon" />
      <span class="toast-message">{{ item.message }}</span>
      <button v-if="item.actionLabel" class="text-button toast-action" type="button" @click="item.onAction?.(); feedback.dismiss(item.id)">{{ item.actionLabel }}</button>
      <button class="toast-close" type="button" aria-label="关闭提示" @click="feedback.dismiss(item.id)"><X :size="14" /></button>
    </article>
  </div>
</template>
