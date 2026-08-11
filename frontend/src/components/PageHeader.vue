<script setup lang="ts">
defineProps<{ eyebrow?: string; title: string; titleId?: string }>();
</script>

<template>
  <header class="page-header">
    <div class="page-header-copy">
      <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
      <h1 :id="titleId">{{ title }}</h1>
      <slot name="description" />
    </div>
    <div v-if="$slots.actions" class="page-header-actions"><slot name="actions" /></div>
  </header>
</template>
