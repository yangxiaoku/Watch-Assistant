<script setup lang="ts">
import { computed } from "vue";
import { ArrowLeft, Film, Heart, RefreshCw, Star } from "@lucide/vue";
import ResourceTable from "../components/ResourceTable.vue";
import SourceDiagnostics from "../components/SourceDiagnostics.vue";
import InlineAlert from "../components/InlineAlert.vue";
import { posterUrl } from "../format";
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
  resourceSearchLoading?: boolean;
  resourceError?: string;
  sourceNames?: string[];
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
// 豆瓣风格星级:vote_average 0-10 → 5 星,支持半星(向上取整显示填满数)。
const ratingStars = computed(() => {
  const vote = props.result.movie.vote_average;
  if (vote == null) return 0;
  return Math.round((vote / 10) * 5);
});
const backdrop = computed(() =>
  props.result.movie.backdrop_path
    ? posterUrl(props.result.movie.backdrop_path, "w1280")
    : null,
);
const seasons = computed(() => props.result.movie.seasons ?? []);
const selectedSeasonNumber = computed(() => props.seasonNumber ?? null);
const selectedSeasonSummary = computed(() => selectedSeasonNumber.value === null
  ? null
  : seasons.value.find((season) => season.season_number === selectedSeasonNumber.value) ?? null);

function displaySeasonName(seasonNumber: number, name?: string | null): string {
  const normalizedName = name?.trim();
  return normalizedName || (seasonNumber === 0 ? "特别篇" : `第 ${seasonNumber} 季`);
}

const selectedSeasonName = computed(() => selectedSeasonNumber.value === null
  ? ""
  : displaySeasonName(selectedSeasonNumber.value, props.seasonDetail?.name || selectedSeasonSummary.value?.name));
const selectedSeasonAirDate = computed(() => props.seasonDetail?.air_date || selectedSeasonSummary.value?.air_date || null);
const selectedSeasonEpisodeCount = computed(() => props.seasonDetail?.episode_count ?? selectedSeasonSummary.value?.episode_count ?? null);
const displayedPosterPath = computed(() => selectedSeasonNumber.value !== null
  ? props.seasonDetail?.poster_path || selectedSeasonSummary.value?.poster_path || props.result.movie.poster_path
  : props.result.movie.poster_path);
const resourceHeading = computed(() => {
  if (!isTv.value) return "可用资源";
  return selectedSeasonNumber.value === null ? "全部季度资源" : `${selectedSeasonName.value}可用资源`;
});
const resourceCounts = computed(() => ({
  magnets: props.resourceFacets?.magnet ?? (props.resources ?? props.result.results).filter((resource) => resource.kind === "magnet").length,
  shares: props.resourceFacets?.share ?? (props.resources ?? props.result.results).filter((resource) => resource.kind === "115_share").length,
  verified: (props.resources ?? props.result.results).filter((resource) => resource.inspection_status === "verified").length,
}));
const sourceWarningPrefixes = ["pansou_query_failed:", "prowlarr_query_failed:"];

function isSourceWarning(value: string): boolean {
  return value === "partial_upstream"
    || value === "prowlarr_unsupported_results"
    || value === "prowlarr_results_truncated"
    || sourceWarningPrefixes.some((prefix) => value.startsWith(prefix));
}

const summaryWarnings = computed(() => props.result.warnings.filter((warning) => !isSourceWarning(warning)));
const sourceIntegrity = computed(() => {
  if (props.result.warnings.some(isSourceWarning)) return "部分来源需核对";
  if (props.sourceNames?.length) return "来源完整";
  return "来源待确认";
});
const cacheSummary = computed(() => {
  if (!props.result.cached) return "本次实时搜索";
  const age = props.result.cache_age_seconds;
  if (age === null || !Number.isFinite(age)) return "使用最近缓存结果";
  if (age < 60) return `使用缓存结果，约 ${Math.max(1, Math.round(age))} 秒前更新`;
  if (age < 3600) return `使用缓存结果，约 ${Math.round(age / 60)} 分钟前更新`;
  return `使用缓存结果，约 ${Math.round(age / 3600)} 小时前更新`;
});

function seasonFromValue(value: string): number | null {
  return value ? Number(value) : null;
}

function onSeasonChange(event: Event): void {
  const target = event.target;
  if (target instanceof HTMLSelectElement) emit("season", seasonFromValue(target.value));
}


function warningLabel(value: string): string {
  if (value.startsWith("pansou_query_failed:")) return "部分搜索请求失败，已保留其他来源结果";
  return {
    alternative_titles_used: "已使用其他译名补充搜索",
    link_check_inconclusive: "部分分享链接暂无法确认，已保留待核对结果",
    new_resources_found: "发现新资源",
    partial_upstream: "部分搜索来源暂不可用，已保留其他来源结果",
    prowlarr_unsupported_results: "Prowlarr 返回了不支持的资源类型，已跳过",
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
    <header class="movie-profile" :class="{ 'has-backdrop': backdrop }" :aria-busy="metadataLoading ? 'true' : 'false'">
      <div v-if="backdrop" class="movie-backdrop" aria-hidden="true"><img :src="backdrop" :alt="`${result.movie.title} 背景`" loading="lazy" /></div>
      <div class="movie-backdrop-scrim" aria-hidden="true"></div>
      <div class="detail-poster">
        <img v-if="posterUrl(displayedPosterPath)" :src="posterUrl(displayedPosterPath)!" :alt="selectedSeasonNumber !== null && seasonDetail && !seasonDetail.poster_path && !selectedSeasonSummary?.poster_path ? `${result.movie.title} 海报（本季暂无独立海报）` : `${result.movie.title} 海报`" />
        <span v-else class="poster-fallback"><Film :size="34" /></span>
      </div>
      <div class="movie-copy"><p class="eyebrow">{{ isTv ? '电视剧' : '电影' }}<span v-if="result.movie.release_year !== null"> · {{ result.movie.release_year }}</span> · TMDB {{ result.movie.tmdb_id }}</p><h1>{{ result.movie.title }}</h1><p v-if="result.movie.original_title" class="original-title">{{ result.movie.original_title }}</p><p v-if="result.movie.vote_average !== null" class="detail-rating"><span class="star-rating" :title="`${result.movie.vote_average.toFixed(1)} / 10`" role="img" :aria-label="`评分 ${result.movie.vote_average.toFixed(1)} / 10`"><Star v-for="n in 5" :key="n" :size="15" :class="{ filled: n <= ratingStars }" :fill="n <= ratingStars ? 'currentColor' : 'none'" /></span><strong>{{ result.movie.vote_average.toFixed(1) }}</strong> / 10</p><div v-if="isTv && seasons.length" class="season-bar"><label for="season-select">季度</label><select id="season-select" :value="selectedSeasonNumber ?? ''" @change="onSeasonChange"><option value="">全部季度</option><option v-for="season in seasons" :key="season.season_number" :value="season.season_number">{{ displaySeasonName(season.season_number, season.name) }} · {{ season.episode_count }} 集</option></select></div><button class="secondary-button favorite-inline" :class="{ active: favorite }" type="button" @click="$emit('favorite')"><Heart :size="16" :fill="favorite ? 'currentColor' : 'none'" />{{ favorite ? '已收藏' : '收藏' }}</button></div>
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
    <section v-if="selectedSeasonNumber !== null" class="overview-section season-detail-section" aria-live="polite"><h2>{{ selectedSeasonName }}</h2><p class="season-detail-meta">{{ selectedSeasonAirDate || '播出日期待定' }} · {{ selectedSeasonEpisodeCount ?? '集数待定' }} 集</p><p v-if="seasonDetail?.overview">{{ seasonDetail.overview }}<span v-if="seasonDetail.overview_language" class="season-detail-source">（{{ seasonDetail.overview_language }}）</span></p><p v-else-if="seasonDetailLoading">正在加载本季资料</p><InlineAlert v-else-if="seasonDetailError" variant="error" :message="seasonDetailError" /><p v-else>本季暂无独立简介</p><p v-if="seasonDetail?.stale" class="season-detail-source">当前显示最近成功缓存的季度资料</p><p v-if="seasonDetail && !seasonDetail.poster_path && !selectedSeasonSummary?.poster_path" class="season-detail-source">本季暂无独立海报，当前使用剧集海报</p></section>
    <section v-else-if="result.movie.overview" class="overview-section"><h2>简介</h2><p>{{ result.movie.overview }}</p></section>
    <section v-else class="overview-section overview-empty"><h2>简介</h2><p class="empty-copy">暂无简介</p></section>
    <InlineAlert v-if="summaryWarnings.length" variant="info" :message="summaryWarnings.map(warningLabel).join(' · ')" />
    <div class="resource-search-summary" aria-live="polite"><span><strong>搜索状态</strong>{{ cacheSummary }}</span><span><strong>来源</strong>{{ sourceNames?.length ? sourceNames.join('、') : '来源信息待确认' }}</span><span :class="sourceIntegrity === '来源完整' ? 'is-complete' : 'is-incomplete'"><strong>完整性</strong>{{ sourceIntegrity }}</span></div>
    <SourceDiagnostics :warnings="result.warnings" />
    <div v-if="selectedSeasonNumber !== null && !resourceLoading && (resourceTotal ?? 0) === 0" class="season-empty">
      <p class="empty-copy">当前季度暂无独立资源</p>
    </div>
    <ResourceTable :resources="resources ?? result.results" :heading="resourceHeading" :facets="resourceFacets" :total="resourceTotal" :hidden-total="resourceHiddenTotal" :page="resourcePage" :page-size="resourcePageSize" :total-pages="resourceTotalPages" :source-names="sourceNames" :resource-kind="resourceKind" :resource-quality="resourceQuality" :resource-query="resourceQuery" :resource-sort="resourceSort" :resource-loading="resourceLoading" :resource-search-loading="resourceSearchLoading" :resource-error="resourceError" :pagination-unavailable="paginationUnavailable" :pushing-id="pushingId" :push-capabilities="pushCapabilities" :inspection-supported="inspectionSupported" :inspection-state="inspectionState" :inspection-completed="inspectionCompleted" :inspection-total="inspectionTotal" :inspection-failed="inspectionFailed" :inspection-error="inspectionError" :inspection-more-available="inspectionMoreAvailable" :inspection-retry-available="inspectionRetryAvailable" :inspection-started="inspectionStarted" @push="$emit('push', $event)" @inspect-more="$emit('inspectMore')" @retry-failed="$emit('retryFailed')" @retry-page="$emit('retryPage')" @page="$emit('page', $event)" @kind="$emit('kind', $event)" @quality="$emit('quality', $event)" @query="$emit('query', $event)" @sort="$emit('sort', $event)" @page-size="$emit('pageSize', $event)" />
  </section>
</template>
