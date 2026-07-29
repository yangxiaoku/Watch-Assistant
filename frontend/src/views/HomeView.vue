<script setup lang="ts">
import { ArrowRight, Heart, Play, Star } from "@lucide/vue";
import { computed } from "vue";
import MovieRow from "../components/MovieRow.vue";
import { mediaKey } from "../media";
import type { HomeCatalogResponse, MovieMetadata } from "../types";

const props = defineProps<{
  catalog: HomeCatalogResponse | null;
  loading: boolean;
  favoriteIds: Set<string>;
}>();
defineEmits<{
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
  navigate: [view: "movies" | "tv" | "popular"];
}>();

const hero = computed(() => props.catalog?.popular.find((movie) => movie.backdrop_path) ?? props.catalog?.popular[0] ?? null);
const highlights = computed(() => props.catalog?.popular.filter((movie) => movie.tmdb_id !== hero.value?.tmdb_id).slice(0, 3) ?? []);

function backdropUrl(path: string | null | undefined): string | null {
  return path ? `https://image.tmdb.org/t/p/w1280${path}` : null;
}

function posterUrl(path: string | null): string | null {
  return path ? `https://image.tmdb.org/t/p/w342${path}` : null;
}
</script>

<template>
  <div v-if="loading && !catalog" class="home-loading" aria-label="正在加载首页"><div class="hero-skeleton" /><div class="row-skeleton"><span v-for="index in 6" :key="index" /></div></div>
  <div v-else-if="catalog && hero" class="home-view">
    <section class="feature-layout" aria-label="热门推荐">
      <button class="feature-hero" type="button" :aria-label="`查看 ${hero.title}`" @click="$emit('open', hero)">
        <img v-if="backdropUrl(hero.backdrop_path)" :src="backdropUrl(hero.backdrop_path)!" :alt="`${hero.title} 剧照`" />
        <span class="feature-shade" />
        <span class="feature-copy">
          <span class="feature-kicker">今日焦点</span>
          <strong>{{ hero.title }}</strong>
          <small v-if="hero.original_title">{{ hero.original_title }}</small>
          <span class="feature-meta"><span v-if="hero.vote_average"><Star :size="14" fill="currentColor" />{{ hero.vote_average.toFixed(1) }}</span><span>{{ hero.release_year ?? '年份未知' }}</span></span>
          <span class="feature-overview">{{ hero.overview || '查看影片详情并聚合可用资源。' }}</span>
          <span class="feature-command"><Play :size="16" fill="currentColor" />查找资源<ArrowRight :size="16" /></span>
        </span>
      </button>
      <aside class="feature-list" aria-label="热门推荐列表">
        <button v-for="movie in highlights" :key="movie.tmdb_id" type="button" class="feature-item" @click="$emit('open', movie)">
          <img v-if="posterUrl(movie.poster_path)" :src="posterUrl(movie.poster_path)!" :alt="`${movie.title} 海报`" />
          <span><strong>{{ movie.title }}</strong><small>{{ movie.original_title }}</small><em><Star :size="12" fill="currentColor" />{{ movie.vote_average?.toFixed(1) ?? '暂无评分' }} · {{ movie.release_year ?? '年份未知' }}</em></span>
        </button>
      </aside>
      <button class="hero-favorite" type="button" :class="{ active: favoriteIds.has(mediaKey(hero)) }" :aria-label="favoriteIds.has(mediaKey(hero)) ? `取消收藏 ${hero.title}` : `收藏 ${hero.title}`" @click.stop="$emit('favorite', hero)">
        <Heart :size="18" :fill="favoriteIds.has(mediaKey(hero)) ? 'currentColor' : 'none'" />
      </button>
    </section>

    <MovieRow title="正在热映" eyebrow="院线新片" :movies="catalog.now_playing" :favorite-ids="favoriteIds" action-label="全部电影" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" @action="$emit('navigate', 'movies')" />
    <MovieRow title="本周热门" eyebrow="本周热度" :movies="catalog.popular" :favorite-ids="favoriteIds" action-label="查看热门" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" @action="$emit('navigate', 'popular')" />
    <MovieRow title="热播剧集" eyebrow="正在热播" :movies="catalog.tv_on_the_air" :favorite-ids="favoriteIds" action-label="全部剧集" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" @action="$emit('navigate', 'tv')" />
    <MovieRow title="即将上映" eyebrow="即将到来" :movies="catalog.upcoming" :favorite-ids="favoriteIds" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
    <MovieRow title="高分佳片" eyebrow="高分推荐" :movies="catalog.top_rated" :favorite-ids="favoriteIds" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
    <MovieRow title="高分剧集" eyebrow="高分剧集" :movies="catalog.tv_top_rated" :favorite-ids="favoriteIds" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
  </div>
  <div v-else class="empty-state">首页内容暂时不可用</div>
</template>
