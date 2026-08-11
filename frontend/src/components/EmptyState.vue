<script setup lang="ts">
import { Film } from "@lucide/vue";

defineProps<{ title: string; hint?: string }>();
</script>

<template>
  <div class="empty-state" role="status">
    <span class="empty-state-mark"><Film :size="22" /></span>
    <strong>{{ title }}</strong>
    <span v-if="hint">{{ hint }}</span>
    <slot />
  </div>
</template>
