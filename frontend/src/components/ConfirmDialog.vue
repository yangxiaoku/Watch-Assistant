<script setup lang="ts">
import { Check, ShieldAlert, X } from "@lucide/vue";
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = withDefaults(defineProps<{
  open: boolean;
  title: string;
  summary: string;
  details?: string[];
  confirmLabel?: string;
  cancelLabel?: string;
  busy?: boolean;
  tone?: "primary" | "danger";
}>(), {
  details: () => [],
  confirmLabel: "确认执行",
  cancelLabel: "取消",
  busy: false,
  tone: "primary",
});

const emit = defineEmits<{
  cancel: [];
  confirm: [];
}>();

const acknowledged = ref(false);
const confirmButton = ref<HTMLButtonElement | null>(null);

function resetAndFocus(open: boolean): void {
  if (!open) return;
  acknowledged.value = false;
  void nextTick(() => confirmButton.value?.focus());
}

function close(): void {
  if (!props.busy) emit("cancel");
}

function handleKeydown(event: KeyboardEvent): void {
  if (props.open && event.key === "Escape") close();
}

watch(() => props.open, resetAndFocus);

onMounted(() => window.addEventListener("keydown", handleKeydown));
onBeforeUnmount(() => window.removeEventListener("keydown", handleKeydown));
</script>

<template>
  <div v-if="open" class="confirm-dialog-backdrop" role="presentation" @click.self="close">
    <section class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-summary">
      <header class="confirm-dialog-header">
        <div class="confirm-dialog-title"><span class="confirm-dialog-icon"><ShieldAlert :size="18" /></span><h2 id="confirm-dialog-title">{{ title }}</h2></div>
        <button class="icon-button" type="button" title="关闭确认框" aria-label="关闭确认框" :disabled="busy" @click="close"><X :size="18" /></button>
      </header>
      <p id="confirm-dialog-summary" class="confirm-dialog-summary">{{ summary }}</p>
      <ul v-if="details.length" class="confirm-dialog-details">
        <li v-for="detail in details" :key="detail">{{ detail }}</li>
      </ul>
      <label class="confirm-dialog-acknowledgement"><input v-model="acknowledged" type="checkbox" :disabled="busy" />我已核对上述摘要，确认继续此操作</label>
      <footer class="confirm-dialog-actions">
        <button class="secondary-button" type="button" :disabled="busy" @click="close">{{ cancelLabel }}</button>
        <button ref="confirmButton" :class="tone === 'danger' ? 'danger-button' : 'primary-button'" type="button" :disabled="busy || !acknowledged" @click="emit('confirm')"><Check :size="16" />{{ busy ? "正在提交" : confirmLabel }}</button>
      </footer>
    </section>
  </div>
</template>
