<script setup lang="ts">
import { ChevronLeft, ChevronRight } from "@lucide/vue";
import { ref } from "vue";
import MovieCard from "./MovieCard.vue";
import { mediaKey } from "../media";
import type { MovieMetadata } from "../types";

const props = defineProps<{
  title: string;
  eyebrow: string;
  movies: MovieMetadata[];
  favoriteIds: Set<string>;
  actionLabel?: string;
}>();
defineEmits<{
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
  action: [];
}>();

const row = ref<HTMLElement | null>(null);

function scrollRow(direction: 1 | -1) {
  const el = row.value;
  if (!el) return;
  el.scrollBy({ left: direction * Math.max(el.clientWidth * 0.8, 320), behavior: "smooth" });
}
</script>

<template>
  <section class="movie-section" :aria-label="title">
    <header class="content-heading">
      <div><p class="eyebrow">{{ eyebrow }}</p><h2>{{ title }}</h2></div>
      <div class="row-actions">
        <button v-if="actionLabel" class="text-button" type="button" @click="$emit('action')">{{ actionLabel }}<ChevronRight :size="16" /></button>
        <div class="row-scroll-controls">
          <button type="button" class="row-scroll-button" aria-label="上一组" @click="scrollRow(-1)"><ChevronLeft :size="16" /></button>
          <button type="button" class="row-scroll-button" aria-label="下一组" @click="scrollRow(1)"><ChevronRight :size="16" /></button>
        </div>
      </div>
    </header>
    <div ref="row" class="movie-row">
      <MovieCard v-for="movie in movies.slice(0, 12)" :key="mediaKey(movie)" :movie="movie" :favorite="favoriteIds.has(mediaKey(movie))" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
    </div>
  </section>
</template>
