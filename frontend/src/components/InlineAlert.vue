<script setup lang="ts">
import { AlertTriangle, CircleAlert, CircleCheck, Info, X } from "@lucide/vue";
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    variant?: "info" | "warning" | "error" | "success";
    title?: string;
    message?: string;
    actionLabel?: string;
    closable?: boolean;
  }>(),
  { variant: "info" },
);
const emit = defineEmits<{ action: []; close: [] }>();

const ICONS = { success: CircleCheck, info: Info, warning: AlertTriangle, error: CircleAlert } as const;
const icon = computed(() => ICONS[props.variant]);
const liveRole = computed(() => (props.variant === "error" || props.variant === "warning" ? "alert" : "status"));
</script>

<template>
  <div class="inline-alert" :class="`inline-alert-${variant}`" :role="liveRole">
    <component :is="icon" :size="16" class="inline-alert-icon" />
    <div class="inline-alert-copy">
      <strong v-if="title" class="inline-alert-title">{{ title }}</strong>
      <span v-if="message" class="inline-alert-message">{{ message }}</span>
      <slot />
    </div>
    <button v-if="actionLabel" class="text-button inline-alert-action" type="button" @click="emit('action')">{{ actionLabel }}</button>
    <button v-if="closable" class="inline-alert-close" type="button" aria-label="关闭提示" @click="emit('close')"><X :size="14" /></button>
  </div>
</template>
