<script setup lang="ts">
import { computed } from "vue";
import { ArrowLeft, Film, Heart, RefreshCw, Star } from "@lucide/vue";
import ResourceTable from "../components/ResourceTable.vue";
import type { PushCapabilities } from "../push";
import type { ResourceFacets, ResourceQuality, ResourceSort, ResourceSummary, SearchResponse, SeasonDetailResponse } from "../types";

const props = defineProps<{
  result: SearchResponse;
  resources?: ResourceSummary[];
  resourceFacets?: ResourceFacets;
  resourceTotal?: number;
  resourceHiddenTotal?: number;
  resourcePage?: number;
  resourcePageSize?: 25 | 50 | 100;
  resourceTotalPages?: number;
  resourceKind?: "all" | "magnet" | "115_share";
  resourceQuality?: "all" | ResourceQuality;
  resourceQuery?: string;
  resourceSort?: ResourceSort;
  resourceLoading?: boolean;
  resourceError?: string;
  metadataLoading?: boolean;
  metadataError?: string;
  metadataStale?: boolean;
  paginationUnavailable?: boolean;
  mediaType?: "movie" | "tv";
  seasonNumber?: number | null;
  seasonDetail?: SeasonDetailResponse | null;
  seasonDetailLoading?: boolean;
  seasonDetailError?: string;
  pushingId: string | null;
  pushCapabilities?: PushCapabilities;
  favorite: boolean;
  inspectionSupported?: boolean;
  inspectionState?: "idle" | "running" | "completed" | "partial" | "failed" | "timeout";
  inspectionCompleted?: number;
  inspectionTotal?: number;
  inspectionFailed?: number;
  inspectionError?: string | null;
  inspectionMoreAvailable?: boolean;
  inspectionRetryAvailable?: boolean;
  inspectionStarted?: boolean;
}>();
const emit = defineEmits<{ push: [resource: ResourceSummary]; refresh: []; retryMetadata: []; back: []; favorite: []; season: [seasonNumber: number | null]; inspectMore: []; retryFailed: []; retryPage: []; page: [page: number]; kind: [kind: "all" | "magnet" | "115_share"]; quality: [quality: ResourceQuality]; query: [query: string]; sort: [sort: ResourceSort]; pageSize: [pageSize: 25 | 50 | 100] }>();

const isTv = computed(() => props.mediaType === "tv" || props.result.movie.media_type === "tv");
const seasons = computed(() => props.result.movie.seasons ?? []);
const displayedPosterPath = computed(() => props.seasonNumber !== null && props.seasonDetail?.poster_path ? props.seasonDetail.poster_path : props.result.movie.poster_path);
const resourceCounts = computed(() => ({
  magnets: props.resourceFacets?.magnet ?? (props.resources ?? props.result.results).filter((resource) => resource.kind === "magnet").length,
  shares: props.resourceFacets?.share ?? (props.resources ?? props.result.results).filter((resource) => resource.kind === "115_share").length,
  verified: (props.resources ?? props.result.results).filter((resource) => resource.inspection_status === "verified").length,
}));

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

function warningLabel(value: string): string {
  if (value.startsWith("pansou_query_failed:")) return "部分搜索请求失败，已保留其他来源结果";
  return {
    alternative_titles_used: "已使用其他译名补充搜索",
    link_check_inconclusive: "部分分享链接暂无法确认，已保留待核对结果",
    new_resources_found: "发现新资源",
    partial_upstream: "部分搜索来源暂不可用，已保留其他来源结果",
    resource_mismatch_filtered: "已隐藏与当前影视不匹配的资源",
    resource_results_truncated: "结果较多，当前仅展示部分资源，可使用分页查看其余结果",
    stale_cache: "部分结果来自缓存，搜索来源未完全可用",
    watching_for_resources: "暂未找到可用资源，系统会继续等待新结果",
  }[value] ?? "搜索结果存在待核对提示";
}
</script>

<template>
  <section class="movie-view">
    <div class="detail-actions"><button class="back-button" type="button" @click="$emit('back')"><ArrowLeft :size="17" />返回浏览</button><button class="secondary-button refresh-button" type="button" @click="$emit('refresh')"><RefreshCw :size="16" />刷新资源</button></div>
    <header class="movie-profile" :aria-busy="metadataLoading ? 'true' : 'false'">
      <div class="detail-poster">
        <img v-if="posterUrl(displayedPosterPath)" :src="posterUrl(displayedPosterPath)!" :alt="seasonNumber !== null && seasonDetail && !seasonDetail.poster_path ? `${result.movie.title} 海报（本季暂无独立海报）` : `${result.movie.title} 海报`" />
        <span v-else class="poster-fallback"><Film :size="34" /></span>
      </div>
      <div class="movie-copy"><p class="eyebrow">{{ isTv ? '电视剧' : '电影' }}<span v-if="result.movie.release_year !== null"> · {{ result.movie.release_year }}</span> · TMDB {{ result.movie.tmdb_id }}</p><h1>{{ result.movie.title }}</h1><p v-if="result.movie.original_title" class="original-title">{{ result.movie.original_title }}</p><p v-if="result.movie.vote_average !== null" class="detail-rating"><Star :size="14" fill="currentColor" />{{ result.movie.vote_average.toFixed(1) }} / 10</p><div v-if="isTv && seasons.length" class="season-bar"><label for="season-select">季度</label><select id="season-select" :value="seasonNumber ?? ''" @change="onSeasonChange"><option value="">全部季度</option><option v-for="season in seasons" :key="season.season_number" :value="season.season_number">{{ season.name || `第 ${season.season_number} 季` }} · {{ season.episode_count }} 集</option></select></div><button class="secondary-button favorite-inline" :class="{ active: favorite }" type="button" @click="$emit('favorite')"><Heart :size="16" :fill="favorite ? 'currentColor' : 'none'" />{{ favorite ? '已收藏' : '收藏' }}</button></div>
      <aside class="detail-stats" aria-label="影片数据">
        <div class="detail-stat score-stat" v-if="result.movie.vote_average !== null"><span>TMDB 评分</span><strong>{{ result.movie.vote_average.toFixed(1) }} / 10</strong></div>
        <div class="detail-stat"><span>磁力</span><strong>{{ resourceCounts.magnets }}</strong></div>
        <div class="detail-stat"><span>115 分享</span><strong>{{ resourceCounts.shares }}</strong></div>
        <div class="detail-stat"><span>已验证（本页）</span><strong>{{ resourceCounts.verified }}</strong></div>
      </aside>
    </header>
    <section v-if="metadataLoading || metadataError || metadataStale" class="metadata-status" :class="{ 'metadata-status-error': metadataError }" :aria-busy="metadataLoading ? 'true' : 'false'">
      <span v-if="metadataLoading" role="status">正在加载影视资料</span>
      <span v-else-if="metadataError" role="alert">影视资料暂时无法加载：{{ metadataError }} <button class="text-button" type="button" @click="emit('retryMetadata')">重试资料</button></span>
      <span v-else>当前显示列表中的影视资料摘要</span>
    </section>
    <section v-if="seasonNumber !== null" class="overview-section season-detail-section" aria-live="polite"><h2>{{ seasonDetail?.name || `第 ${seasonNumber} 季` }}</h2><p class="season-detail-meta">{{ seasonDetail?.air_date || '播出日期待定' }} · {{ seasonDetail?.episode_count ?? '集数待定' }} 集</p><p v-if="seasonDetail?.overview">{{ seasonDetail.overview }}<span v-if="seasonDetail.overview_language" class="season-detail-source">（{{ seasonDetail.overview_language }}）</span></p><p v-else-if="seasonDetailLoading">正在加载本季资料</p><p v-else-if="seasonDetailError" class="error-text">{{ seasonDetailError }}</p><p v-else>本季暂无独立简介</p><p v-if="seasonDetail?.stale" class="season-detail-source">当前显示最近成功缓存的季度资料</p><p v-if="seasonDetail && !seasonDetail.poster_path" class="season-detail-source">本季暂无独立海报，当前使用剧集海报</p></section>
    <section v-else-if="result.movie.overview" class="overview-section"><h2>简介</h2><p>{{ result.movie.overview }}</p></section>
    <div v-if="result.warnings.length" class="warning-strip">{{ result.warnings.map(warningLabel).join(' · ') }}</div>
    <ResourceTable :resources="resources ?? result.results" :facets="resourceFacets" :total="resourceTotal" :hidden-total="resourceHiddenTotal" :page="resourcePage" :page-size="resourcePageSize" :total-pages="resourceTotalPages" :resource-kind="resourceKind" :resource-quality="resourceQuality" :resource-query="resourceQuery" :resource-sort="resourceSort" :resource-loading="resourceLoading" :resource-error="resourceError" :pagination-unavailable="paginationUnavailable" :pushing-id="pushingId" :push-capabilities="pushCapabilities" :inspection-supported="inspectionSupported" :inspection-state="inspectionState" :inspection-completed="inspectionCompleted" :inspection-total="inspectionTotal" :inspection-failed="inspectionFailed" :inspection-error="inspectionError" :inspection-more-available="inspectionMoreAvailable" :inspection-retry-available="inspectionRetryAvailable" :inspection-started="inspectionStarted" @push="$emit('push', $event)" @inspect-more="$emit('inspectMore')" @retry-failed="$emit('retryFailed')" @retry-page="$emit('retryPage')" @page="$emit('page', $event)" @kind="$emit('kind', $event)" @quality="$emit('quality', $event)" @query="$emit('query', $event)" @sort="$emit('sort', $event)" @page-size="$emit('pageSize', $event)" />
  </section>
</template>
