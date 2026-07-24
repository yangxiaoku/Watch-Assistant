<script setup lang="ts">
import { Film, Heart, Star } from "@lucide/vue";
import type { MovieMetadata } from "../types";

defineProps<{ movie: MovieMetadata; favorite?: boolean }>();
defineEmits<{ open: [movie: MovieMetadata]; favorite: [movie: MovieMetadata] }>();

function posterUrl(path: string | null): string | null {
  return path ? `https://image.tmdb.org/t/p/w500${path}` : null;
}
</script>

<template>
  <article class="movie-tile">
    <button class="movie-card" type="button" :aria-label="`查看 ${movie.title}`" @click="$emit('open', movie)">
      <span class="poster-frame">
        <img v-if="posterUrl(movie.poster_path)" :src="posterUrl(movie.poster_path)!" :alt="`${movie.title} 海报`" loading="lazy" />
        <span v-else class="poster-fallback"><Film :size="30" /></span>
        <span v-if="movie.vote_average" class="rating"><Star :size="12" fill="currentColor" />{{ movie.vote_average.toFixed(1) }}</span>
        <span v-if="movie.media_type === 'tv'" class="media-badge">剧集</span>
      </span>
      <span class="movie-card-copy"><strong>{{ movie.title }}</strong><small>{{ movie.release_year ?? '年份未知' }}<template v-if="movie.original_title"> · {{ movie.original_title }}</template></small></span>
    </button>
    <button class="favorite-button" type="button" :class="{ active: favorite }" :aria-label="favorite ? `取消收藏 ${movie.title}` : `收藏 ${movie.title}`" :title="favorite ? '取消收藏' : '收藏'" @click="$emit('favorite', movie)">
      <Heart :size="15" :fill="favorite ? 'currentColor' : 'none'" />
    </button>
  </article>
</template>
