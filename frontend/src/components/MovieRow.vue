<script setup lang="ts">
import { ChevronRight } from "@lucide/vue";
import MovieCard from "./MovieCard.vue";
import type { MovieMetadata } from "../types";

defineProps<{
  title: string;
  eyebrow: string;
  movies: MovieMetadata[];
  favoriteIds: Set<number>;
  actionLabel?: string;
}>();
defineEmits<{
  open: [movie: MovieMetadata];
  favorite: [movie: MovieMetadata];
  action: [];
}>();
</script>

<template>
  <section class="movie-section" :aria-label="title">
    <header class="content-heading">
      <div><p class="eyebrow">{{ eyebrow }}</p><h2>{{ title }}</h2></div>
      <button v-if="actionLabel" class="text-button" type="button" @click="$emit('action')">{{ actionLabel }}<ChevronRight :size="16" /></button>
    </header>
    <div class="movie-row">
      <MovieCard v-for="movie in movies.slice(0, 12)" :key="movie.tmdb_id" :movie="movie" :favorite="favoriteIds.has(movie.tmdb_id)" @open="$emit('open', $event)" @favorite="$emit('favorite', $event)" />
    </div>
  </section>
</template>
