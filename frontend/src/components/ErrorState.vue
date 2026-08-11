<script setup lang="ts">
import { TriangleAlert } from "@lucide/vue";

defineProps<{ title: string; message?: string }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div class="error-state" role="alert">
    <span class="empty-state-mark"><TriangleAlert :size="22" /></span>
    <strong>{{ title }}</strong>
    <span v-if="message">{{ message }}</span>
    <slot />
  </div>
</template>
