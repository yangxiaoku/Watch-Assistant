<script setup lang="ts">
import { Clock3, Heart } from "@lucide/vue";
import MovieCard from "../components/MovieCard.vue";
import { mediaKey } from "../media";
import type { MovieMetadata } from "../types";

defineProps<{
  mode: "favorites" | "history";
  movies: MovieMetadata[];
  favoriteIds: Set<string>;
}>();
defineEmits<{ open: [movie: MovieMetadata]; favorite: [movie: MovieMetadata] }>();
</script>

<template>
  <section class="collection-view">
    <header class="library-heading"><div><p class="eyebrow">YOUR LIBRARY</p><h1>{{ mode === 'favorites' ? '我的收藏' : '观看记录' }}</h1><p>{{ mode === 'favorites' ? '保存在当前浏览器中的收藏影片。' : '最近打开过的影片会自动保留在这里。' }}</p></div><Heart v-if="mode === 'favorites'" :size="28" /><Clock3 v-else :size="28" /></header>
    <div v-if="movies.length" class="movie-grid catalog-grid"><MovieCard v-for="movie in movies" :key="mediaKey(movie)" :movie="movie" :favorite="favoriteIds.has(mediaKey(movie))" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" /></div>
    <div v-else class="collection-empty"><Heart v-if="mode === 'favorites'" :size="30" /><Clock3 v-else :size="30" /><strong>{{ mode === 'favorites' ? '还没有收藏' : '还没有观看记录' }}</strong><span>{{ mode === 'favorites' ? '在影片卡片上点击心形按钮即可收藏。' : '打开任意影片后会自动出现在这里。' }}</span></div>
  </section>
</template>
