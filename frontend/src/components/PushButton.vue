<script setup lang="ts">
import { LoaderCircle, Send } from "@lucide/vue";

const props = withDefaults(defineProps<{ busy?: boolean; disabled?: boolean; title?: string }>(), {
  title: "推送到 115",
});
defineEmits<{ push: [] }>();
</script>

<template>
  <button
    class="push-button"
    type="button"
    :disabled="disabled || busy"
    :title="props.title"
    @click="$emit('push')"
  >
    <LoaderCircle v-if="busy" class="spin" :size="16" />
    <Send v-else :size="16" />
    <span>{{ busy ? "处理中" : "推送下载" }}</span>
  </button>
</template>
