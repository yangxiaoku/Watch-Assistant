<script setup lang="ts">
import { SearchX } from "@lucide/vue";
import MovieCard from "../components/MovieCard.vue";
import type { MovieMetadata } from "../types";

defineProps<{
  modelValue: string;
  loading: boolean;
  movies: MovieMetadata[];
  heading: string;
  favoriteIds: Set<number>;
}>();
defineEmits<{
  "update:modelValue": [value: string];
  search: [];
  reset: [];
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
}>();
</script>

<template>
  <section class="search-view" aria-labelledby="catalog-title">
    <header class="library-heading search-heading">
      <div><p class="eyebrow">SEARCH RESULTS</p><h1 id="catalog-title">{{ heading }}</h1><p>从 TMDB 结果中选择影片，随后自动查询 PanSou 资源。</p></div>
      <button class="secondary-button" type="button" @click="$emit('reset')">返回首页</button>
    </header>
    <div v-if="loading" class="movie-grid" aria-label="正在加载电影">
      <div v-for="index in 12" :key="index" class="movie-skeleton"><span /></div>
    </div>
    <div v-else-if="movies.length" class="movie-grid">
      <MovieCard v-for="movie in movies" :key="movie.tmdb_id" :movie="movie" :favorite="favoriteIds.has(movie.tmdb_id)" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
    </div>
    <div v-else class="collection-empty"><SearchX :size="30" /><strong>没有找到相关电影</strong><span>尝试使用更简短的中文名或英文原名。</span></div>
  </section>
</template>
