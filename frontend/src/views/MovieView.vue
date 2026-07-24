<script setup lang="ts">
import { computed } from "vue";
import { ArrowLeft, Film, Heart, RefreshCw, Star } from "@lucide/vue";
import ResourceTable from "../components/ResourceTable.vue";
import type { ResourceSummary, SearchResponse } from "../types";

const props = defineProps<{
  result: SearchResponse;
  mediaType?: "movie" | "tv";
  seasonNumber?: number | null;
  pushingId: string | null;
  pushSupported: boolean;
  favorite: boolean;
  inspectionSupported?: boolean;
  inspectionState?: "idle" | "running" | "completed" | "partial" | "failed" | "timeout";
  inspectionCompleted?: number;
  inspectionTotal?: number;
  inspectionFailed?: number;
  inspectionError?: string | null;
}>();
const emit = defineEmits<{ push: [resource: ResourceSummary]; refresh: []; back: []; favorite: []; season: [seasonNumber: number | null]; inspect: [] }>();

const isTv = computed(() => props.mediaType === "tv" || props.result.movie.media_type === "tv");
const seasons = computed(() => props.result.movie.seasons ?? []);

function seasonFromValue(value: string): number | null {
  return value ? Number(value) : null;
}

function onSeasonChange(event: Event): void {
  const target = event.target;
  if (target instanceof HTMLSelectElement) emit("season", seasonFromValue(target.value));
}

function posterUrl(path: string | null): string | null {
  return path ? `https://image.tmdb.org/t/p/w500${path}` : null;
}
</script>

<template>
  <section class="movie-view">
    <div class="detail-actions"><button class="back-button" type="button" @click="$emit('back')"><ArrowLeft :size="17" />返回浏览</button><div class="detail-action-group"><button class="secondary-button" :class="{ active: favorite }" type="button" @click="$emit('favorite')"><Heart :size="16" :fill="favorite ? 'currentColor' : 'none'" />{{ favorite ? '已收藏' : '收藏' }}</button><button class="secondary-button" type="button" @click="$emit('refresh')"><RefreshCw :size="16" />刷新资源</button></div></div>
    <header class="movie-profile">
      <div class="detail-poster">
        <img v-if="posterUrl(result.movie.poster_path)" :src="posterUrl(result.movie.poster_path)!" :alt="`${result.movie.title} 海报`" />
        <span v-else class="poster-fallback"><Film :size="34" /></span>
      </div>
      <div class="movie-copy"><p class="eyebrow">{{ isTv ? '电视剧' : '电影' }} · TMDB {{ result.movie.tmdb_id }} · {{ result.movie.release_year ?? '年份未知' }}</p><h1>{{ result.movie.title }}</h1><p v-if="result.movie.original_title" class="original-title">{{ result.movie.original_title }}</p><p v-if="result.movie.vote_average" class="detail-rating"><Star :size="14" fill="currentColor" />{{ result.movie.vote_average.toFixed(1) }} / 10</p><p v-if="result.movie.overview" class="movie-overview">{{ result.movie.overview }}</p></div>
    </header>
    <div v-if="isTv && seasons.length" class="season-bar">
      <label for="season-select">季度</label>
      <select id="season-select" :value="seasonNumber ?? ''" @change="onSeasonChange">
        <option value="">全部季度</option>
        <option v-if="seasonNumber === 0 && !seasons.some((season) => season.season_number === 0)" value="0">第 0 季</option>
        <option v-for="season in seasons" :key="season.season_number" :value="season.season_number">
          {{ season.name || `第 ${season.season_number} 季` }} · {{ season.episode_count }} 集
        </option>
      </select>
    </div>
    <div v-if="result.warnings.length" class="warning-strip">{{ result.warnings.join(' · ') }}</div>
    <ResourceTable :resources="result.results" :pushing-id="pushingId" :push-supported="pushSupported" :inspection-supported="inspectionSupported" :inspection-state="inspectionState" :inspection-completed="inspectionCompleted" :inspection-total="inspectionTotal" :inspection-failed="inspectionFailed" :inspection-error="inspectionError" @push="$emit('push', $event)" @inspect="$emit('inspect')" />
  </section>
</template>
