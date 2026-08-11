<script setup lang="ts">
import { computed } from "vue";
import { Film, Heart } from "@lucide/vue";
import { posterUrl } from "../format";
import type { MovieMetadata } from "../types";

const props = defineProps<{ movie: MovieMetadata; favorite?: boolean }>();
defineEmits<{ open: [movie: MovieMetadata]; favorite: [movie: MovieMetadata] }>();

const score = computed(() =>
  props.movie.vote_average != null ? props.movie.vote_average / 10 : null,
);
const scoreStyle = computed(() => {
  if (score.value == null) return {};
  const percent = score.value * 100;
  const color = score.value >= 0.7 ? "var(--mint)" : score.value >= 0.5 ? "var(--gold)" : "var(--danger)";
  return {
    background: `conic-gradient(${color} ${percent * 3.6}deg, rgb(45 55 80 / 55%) ${percent * 3.6}deg)`,
    color,
  };
});
</script>

<template>
  <article class="movie-tile">
    <button class="movie-card" type="button" :aria-label="`查看 ${movie.title}`" @click="$emit('open', movie)">
      <span class="poster-frame">
        <img v-if="posterUrl(movie.poster_path)" :src="posterUrl(movie.poster_path)!" :alt="`${movie.title} 海报`" loading="lazy" />
        <span v-else class="poster-fallback"><Film :size="30" /></span>
        <span
          v-if="movie.vote_average != null"
          class="rating-circle"
          :class="{ 'rating-low': (score ?? 0) < 0.5, 'rating-mid': (score ?? 0) >= 0.5 && (score ?? 0) < 0.7 }"
          :style="scoreStyle"
          :title="`TMDB 评分 ${movie.vote_average.toFixed(1)} / 10`"
          role="img"
          :aria-label="`评分 ${movie.vote_average.toFixed(1)}`"
        ><span class="rating-circle-inner">{{ movie.vote_average.toFixed(1) }}</span></span>
        <span v-else class="rating-none" title="暂无评分">—</span>
        <span v-if="movie.media_type === 'tv'" class="media-badge">剧集</span>
      </span>
      <span class="movie-card-copy"><strong>{{ movie.title }}</strong><small>{{ movie.release_year ?? '年份未知' }}<template v-if="movie.original_title"> · {{ movie.original_title }}</template></small></span>
    </button>
    <button class="favorite-button" type="button" :class="{ active: favorite }" :aria-label="favorite ? `取消收藏 ${movie.title}` : `收藏 ${movie.title}`" :title="favorite ? '取消收藏' : '收藏'" @click="$emit('favorite', movie)">
      <Heart :size="15" :fill="favorite ? 'currentColor' : 'none'" />
    </button>
  </article>
</template>
