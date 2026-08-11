<script setup lang="ts">
import { NAV_GROUPS } from "../nav";
import type { BrowseView } from "../router";

defineProps<{
  activeView: BrowseView;
  organizationPlanEnabled: boolean;
  mobileOpen?: boolean;
}>();

const emit = defineEmits<{
  navigate: [view: BrowseView];
}>();
</script>

<template>
  <nav class="app-sidebar" :class="{ 'mobile-open': mobileOpen }" aria-label="主导航">
    <a class="sidebar-brand" href="/"><span class="brand">WATCH<span>/</span>ASSISTANT</span></a>
    <div class="sidebar-nav">
      <div v-for="group in NAV_GROUPS" :key="group.label" class="sidebar-group" :aria-label="group.label">
        <p class="sidebar-group-label">{{ group.label }}</p>
        <template v-for="item in group.items" :key="item.view">
          <button
            v-if="item.view !== 'organization' || organizationPlanEnabled"
            type="button"
            class="sidebar-item"
            :class="{ active: activeView === item.view }"
            @click="emit('navigate', item.view)"
          >
            <span class="sidebar-item-light" aria-hidden="true" />
            <component :is="item.icon" :size="16" />
            <span class="sidebar-item-label">{{ item.label }}</span>
          </button>
        </template>
      </div>
    </div>
    <div class="sidebar-footer"><slot name="footer" /></div>
  </nav>
</template>
