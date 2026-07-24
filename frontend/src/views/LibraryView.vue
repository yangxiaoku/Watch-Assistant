<script setup lang="ts">
import { SlidersHorizontal } from "@lucide/vue";
import MovieCard from "../components/MovieCard.vue";
import type { MovieMetadata } from "../types";

defineProps<{
  movies: MovieMetadata[];
  loading: boolean;
  favoriteIds: Set<number>;
  genreId?: number;
  year?: number;
  sort: "popular" | "rating" | "release";
}>();
defineEmits<{
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
  filters: [filters: { genreId?: number; year?: number; sort: "popular" | "rating" | "release" }];
}>();

const genres = [
  [undefined, "全部"], [28, "动作"], [12, "冒险"], [16, "动画"], [35, "喜剧"], [80, "犯罪"],
  [99, "纪录"], [18, "剧情"], [10751, "家庭"], [14, "奇幻"], [36, "历史"], [27, "恐怖"],
  [10402, "音乐"], [9648, "悬疑"], [10749, "爱情"], [878, "科幻"], [53, "惊悚"], [10752, "战争"],
] as const;
const currentYear = new Date().getFullYear();
const years = [undefined, ...Array.from({ length: 8 }, (_, index) => currentYear - index)];

</script>

<template>
  <section class="library-view">
    <header class="library-heading"><div><p class="eyebrow">EXPLORE TMDB</p><h1>电影库</h1><p>按类型、年份和排序方式发现电影，打开后直接聚合 PanSou 资源。</p></div><SlidersHorizontal :size="28" /></header>
    <div class="filter-panel">
      <div class="filter-row"><strong>类型</strong><div class="filter-options"><button v-for="genre in genres" :key="genre[1]" type="button" :class="{ active: genreId === genre[0] }" @click="$emit('filters', { genreId: genre[0], year, sort })">{{ genre[1] }}</button></div></div>
      <div class="filter-row"><strong>年份</strong><div class="filter-options"><button v-for="item in years" :key="item ?? 'all'" type="button" :class="{ active: year === item }" @click="$emit('filters', { genreId, year: item, sort })">{{ item ?? '全部' }}</button></div></div>
      <div class="filter-row"><strong>排序</strong><div class="filter-options"><button type="button" :class="{ active: sort === 'popular' }" @click="$emit('filters', { genreId, year, sort: 'popular' })">热度优先</button><button type="button" :class="{ active: sort === 'rating' }" @click="$emit('filters', { genreId, year, sort: 'rating' })">评分优先</button><button type="button" :class="{ active: sort === 'release' }" @click="$emit('filters', { genreId, year, sort: 'release' })">最新上映</button></div></div>
    </div>
    <div v-if="loading" class="movie-grid catalog-grid" aria-label="正在加载电影"><div v-for="index in 12" :key="index" class="movie-skeleton"><span /></div></div>
    <div v-else-if="movies.length" class="movie-grid catalog-grid"><MovieCard v-for="movie in movies" :key="movie.tmdb_id" :movie="movie" :favorite="favoriteIds.has(movie.tmdb_id)" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" /></div>
    <div v-else class="empty-state">当前筛选没有找到电影</div>
  </section>
</template>
