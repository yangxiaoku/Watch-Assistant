<script setup lang="ts">
import { SlidersHorizontal } from "@lucide/vue";
import MovieCard from "../components/MovieCard.vue";
import PaginationBar from "../components/PaginationBar.vue";
import { mediaKey } from "../media";
import type { MovieMetadata } from "../types";

defineProps<{
  movies: MovieMetadata[];
  loading: boolean;
  favoriteIds: Set<string>;
  genreId?: number;
  year?: number;
  sort: "popular" | "rating" | "release";
  mediaType: "movie" | "tv";
  page: number;
  totalPages: number;
  totalResults: number;
}>();
defineEmits<{
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
  filters: [filters: { genreId?: number; year?: number; sort: "popular" | "rating" | "release" }];
  page: [page: number];
}>();

const movieGenres = [
  [undefined, "全部"], [28, "动作"], [12, "冒险"], [16, "动画"], [35, "喜剧"], [80, "犯罪"],
  [99, "纪录"], [18, "剧情"], [10751, "家庭"], [14, "奇幻"], [36, "历史"], [27, "恐怖"],
  [10402, "音乐"], [9648, "悬疑"], [10749, "爱情"], [878, "科幻"], [53, "惊悚"], [10752, "战争"],
] as const;
const tvGenres = [
  [undefined, "全部"], [10759, "动作冒险"], [16, "动画"], [35, "喜剧"], [80, "犯罪"],
  [99, "纪录"], [18, "剧情"], [10751, "家庭"], [10762, "儿童"], [9648, "悬疑"],
  [10763, "新闻"], [10764, "真人秀"], [10765, "科幻奇幻"], [10766, "肥皂剧"],
  [10767, "脱口秀"], [10768, "战争政治"], [37, "西部"],
] as const;
const currentYear = new Date().getFullYear();
const years = [undefined, ...Array.from({ length: 8 }, (_, index) => currentYear - index)];

</script>

<template>
  <section class="library-view">
    <header class="library-heading"><div><p class="eyebrow">EXPLORE TMDB</p><h1 id="catalog-title">{{ mediaType === 'tv' ? '剧集库' : '电影库' }}</h1><p>按类型、年份和排序方式发现{{ mediaType === 'tv' ? '电视剧' : '电影' }}，打开后直接聚合 PanSou 资源。</p></div><SlidersHorizontal :size="28" /></header>
    <div class="filter-panel">
      <div class="filter-row"><strong>类型</strong><div class="filter-options"><button v-for="genre in (mediaType === 'tv' ? tvGenres : movieGenres)" :key="genre[1]" type="button" :class="{ active: genreId === genre[0] }" @click="$emit('filters', { genreId: genre[0], year, sort })">{{ genre[1] }}</button></div></div>
      <div class="filter-row"><strong>年份</strong><div class="filter-options"><button v-for="item in years" :key="item ?? 'all'" type="button" :class="{ active: year === item }" @click="$emit('filters', { genreId, year: item, sort })">{{ item ?? '全部' }}</button></div></div>
      <div class="filter-row"><strong>排序</strong><div class="filter-options"><button type="button" :class="{ active: sort === 'popular' }" @click="$emit('filters', { genreId, year, sort: 'popular' })">热度优先</button><button type="button" :class="{ active: sort === 'rating' }" @click="$emit('filters', { genreId, year, sort: 'rating' })">评分优先</button><button type="button" :class="{ active: sort === 'release' }" @click="$emit('filters', { genreId, year, sort: 'release' })">最新上映</button></div></div>
    </div>
    <div v-if="loading && !movies.length" class="movie-grid catalog-grid" :aria-label="`正在加载${mediaType === 'tv' ? '电视剧' : '电影'}`"><div v-for="index in 12" :key="index" class="movie-skeleton"><span /></div></div>
    <div v-else-if="movies.length" class="catalog-results" :aria-busy="loading ? 'true' : 'false'"><div class="movie-grid catalog-grid"><MovieCard v-for="movie in movies" :key="mediaKey(movie)" :movie="movie" :favorite="favoriteIds.has(mediaKey(movie))" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" /></div><p v-if="loading" class="catalog-loading" role="status">正在加载第 {{ page }} 页</p></div>
    <div v-else class="empty-state">当前筛选没有找到{{ mediaType === 'tv' ? '电视剧' : '电影' }}</div>
    <PaginationBar :page="page" :total-pages="totalPages" :total-results="totalResults" :loading="loading" @page="$emit('page', $event)" />
  </section>
</template>
