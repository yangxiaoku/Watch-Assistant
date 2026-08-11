<script setup lang="ts">
import { Search } from "@lucide/vue";

defineProps<{
  searchInput: string;
}>();

const emit = defineEmits<{
  "update:searchInput": [value: string];
  search: [];
}>();
</script>

<template>
  <header class="app-topbar">
    <form class="top-search" role="search" @submit.prevent="emit('search')">
      <Search :size="17" />
      <input
        :value="searchInput"
        type="search"
        aria-label="搜索电影或电视剧"
        placeholder="搜索电影或电视剧"
        @input="emit('update:searchInput', ($event.target as HTMLInputElement).value)"
      />
      <button type="submit" aria-label="提交搜索" title="搜索"><Search :size="17" /></button>
    </form>
    <div class="topbar-actions"><slot /></div>
  </header>
</template>
