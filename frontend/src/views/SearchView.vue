<script setup lang="ts">
import { Search } from "@lucide/vue";

defineProps<{ modelValue: string; loading: boolean }>();
defineEmits<{ "update:modelValue": [value: string]; search: []; refresh: [] }>();
</script>

<template>
  <section class="search-hero">
    <div><p class="eyebrow">PERSONAL WATCH ASSISTANT / 01</p><h1>找到下一部要看的电影</h1><p class="hero-copy">输入 TMDB 电影 ID，聚合 PanSou 的磁力与 115 分享资源。</p></div>
    <form class="search-form" @submit.prevent="$emit('search')">
      <label for="tmdb-id">TMDB 电影 ID</label>
      <div class="search-input-row"><input id="tmdb-id" :value="modelValue" inputmode="numeric" placeholder="例如 27205" @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)" /><button class="primary-button" type="submit" :disabled="loading"><Search :size="18" />{{ loading ? '搜索中' : '搜索资源' }}</button></div>
    </form>
  </section>
</template>
