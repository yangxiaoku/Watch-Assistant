<script setup lang="ts">
import { Film, Search, Star } from "@lucide/vue";
import type { MovieMetadata } from "../types";

defineProps<{
  modelValue: string;
  loading: boolean;
  movies: MovieMetadata[];
  heading: string;
}>();
defineEmits<{
  "update:modelValue": [value: string];
  search: [];
  reset: [];
  open: [movie: MovieMetadata];
}>();

function posterUrl(path: string | null): string | null {
  return path ? `https://image.tmdb.org/t/p/w500${path}` : null;
}
</script>

<template>
  <section class="browse-hero">
    <div class="browse-intro">
      <p class="eyebrow">DISCOVER / WATCH / SAVE</p>
      <h1>今晚看什么</h1>
      <p class="hero-copy">浏览当前热门电影，选中后自动聚合 PanSou 资源。</p>
    </div>
    <form class="catalog-search" @submit.prevent="$emit('search')">
      <label class="sr-only" for="movie-query">搜索电影</label>
      <Search :size="18" />
      <input id="movie-query" :value="modelValue" type="search" placeholder="搜索电影名称" @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
      <button class="primary-button" type="submit" :disabled="loading">{{ loading ? '搜索中' : '搜索' }}</button>
    </form>
  </section>

  <section class="catalog-section" aria-labelledby="catalog-title">
    <header class="section-heading">
      <div><p class="eyebrow">TMDB MOVIES</p><h2 id="catalog-title">{{ heading }}</h2></div>
      <button v-if="modelValue" class="text-button" type="button" @click="$emit('reset')">查看热门</button>
    </header>
    <div v-if="loading" class="movie-grid" aria-label="正在加载电影">
      <div v-for="index in 12" :key="index" class="movie-skeleton"><span /></div>
    </div>
    <div v-else-if="movies.length" class="movie-grid">
      <button v-for="movie in movies" :key="movie.tmdb_id" class="movie-card" type="button" :aria-label="`查看 ${movie.title}`" @click="$emit('open', movie)">
        <span class="poster-frame">
          <img v-if="posterUrl(movie.poster_path)" :src="posterUrl(movie.poster_path)!" :alt="`${movie.title} 海报`" loading="lazy" />
          <span v-else class="poster-fallback"><Film :size="30" /></span>
          <span v-if="movie.vote_average" class="rating"><Star :size="12" fill="currentColor" />{{ movie.vote_average.toFixed(1) }}</span>
        </span>
        <span class="movie-card-copy"><strong>{{ movie.title }}</strong><small>{{ movie.release_year ?? '年份未知' }}</small></span>
      </button>
    </div>
    <div v-else class="empty-state">没有找到相关电影</div>
  </section>
</template>
