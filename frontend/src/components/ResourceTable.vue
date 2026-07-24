<script setup lang="ts">
import { computed, ref } from "vue";
import { Magnet, PackageOpen } from "@lucide/vue";

import PushButton from "./PushButton.vue";
import type { ResourceKind, ResourceSummary } from "../types";

const props = defineProps<{
  resources: ResourceSummary[];
  pushingId?: string | null;
}>();
const emit = defineEmits<{ push: [resource: ResourceSummary] }>();
const filter = ref<"all" | ResourceKind>("all");
const filteredResources = computed(() =>
  filter.value === "all"
    ? props.resources
    : props.resources.filter((resource) => resource.kind === filter.value),
);

function formatSize(value: number | null): string {
  if (value === null) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(
    new Date(value),
  );
}
</script>

<template>
  <section class="resource-surface">
    <div class="resource-toolbar">
      <div>
        <p class="eyebrow">PanSou 聚合</p>
        <h2>可用资源 <span>{{ filteredResources.length }}</span></h2>
      </div>
      <div class="segmented" role="group" aria-label="资源类型">
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
        <button :class="{ active: filter === 'magnet' }" @click="filter = 'magnet'">
          <Magnet :size="14" /> 磁力
        </button>
        <button :class="{ active: filter === '115_share' }" @click="filter = '115_share'">
          <PackageOpen :size="14" /> 115 分享
        </button>
      </div>
    </div>

    <div v-if="filteredResources.length" class="resource-table-wrap">
      <table class="resource-table">
        <thead>
          <tr><th>资源</th><th>类型</th><th>大小</th><th>做种</th><th>来源 / 抓取时间</th><th /></tr>
        </thead>
        <tbody>
          <tr v-for="resource in filteredResources" :key="resource.resource_id">
            <td class="resource-name">{{ resource.name }}</td>
            <td><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span></td>
            <td class="numeric">{{ formatSize(resource.size_bytes) }}</td>
            <td class="numeric">{{ resource.seeders ?? '未知' }}</td>
            <td><strong>{{ resource.source }}</strong><small>{{ formatDate(resource.captured_at) }}</small></td>
            <td class="action-cell"><PushButton :busy="pushingId === resource.resource_id" @push="emit('push', resource)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty-state">当前筛选没有资源</div>

    <div class="resource-cards">
      <article v-for="resource in filteredResources" :key="resource.resource_id" class="resource-card">
        <div class="card-heading"><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span><span>{{ resource.source }}</span></div>
        <h3>{{ resource.name }}</h3>
        <dl><div><dt>大小</dt><dd>{{ formatSize(resource.size_bytes) }}</dd></div><div><dt>做种</dt><dd>{{ resource.seeders ?? '未知' }}</dd></div><div><dt>抓取</dt><dd>{{ formatDate(resource.captured_at) }}</dd></div></dl>
        <PushButton :busy="pushingId === resource.resource_id" @push="emit('push', resource)" />
      </article>
    </div>
  </section>
</template>
