<script setup lang="ts">
import { ArrowLeft, Film, RefreshCw, Star } from "@lucide/vue";
import ResourceTable from "../components/ResourceTable.vue";
import type { ResourceSummary, SearchResponse } from "../types";

defineProps<{ result: SearchResponse; pushingId: string | null; pushSupported: boolean }>();
defineEmits<{ push: [resource: ResourceSummary]; refresh: []; back: [] }>();

function posterUrl(path: string | null): string | null {
  return path ? `https://image.tmdb.org/t/p/w500${path}` : null;
}
</script>

<template>
  <section class="movie-view">
    <div class="detail-actions"><button class="back-button" type="button" @click="$emit('back')"><ArrowLeft :size="17" />返回热门</button><button class="secondary-button" type="button" @click="$emit('refresh')"><RefreshCw :size="16" />刷新资源</button></div>
    <header class="movie-profile">
      <div class="detail-poster">
        <img v-if="posterUrl(result.movie.poster_path)" :src="posterUrl(result.movie.poster_path)!" :alt="`${result.movie.title} 海报`" />
        <span v-else class="poster-fallback"><Film :size="34" /></span>
      </div>
      <div class="movie-copy"><p class="eyebrow">TMDB {{ result.movie.tmdb_id }} · {{ result.movie.release_year ?? '年份未知' }}</p><h1>{{ result.movie.title }}</h1><p v-if="result.movie.original_title" class="original-title">{{ result.movie.original_title }}</p><p v-if="result.movie.vote_average" class="detail-rating"><Star :size="14" fill="currentColor" />{{ result.movie.vote_average.toFixed(1) }} / 10</p><p v-if="result.movie.overview" class="movie-overview">{{ result.movie.overview }}</p></div>
    </header>
    <div v-if="result.warnings.length" class="warning-strip">{{ result.warnings.join(' · ') }}</div>
    <ResourceTable :resources="result.results" :pushing-id="pushingId" :push-supported="pushSupported" @push="$emit('push', $event)" />
  </section>
</template>
