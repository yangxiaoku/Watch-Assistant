<script setup lang="ts">
defineSlots<{
  sidebar?: () => unknown;
  topbar?: () => unknown;
  default?: () => unknown;
}>();
</script>

<template>
  <div class="app-shell">
    <div class="app-layout">
      <slot name="sidebar" />
      <div class="app-main">
        <slot name="topbar" />
        <slot />
      </div>
    </div>
  </div>
</template>
