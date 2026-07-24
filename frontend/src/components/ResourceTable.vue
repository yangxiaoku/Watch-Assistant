<script setup lang="ts">
import { computed, ref } from "vue";
import { LoaderCircle, Magnet, PackageOpen, ScanSearch } from "@lucide/vue";

import PushButton from "./PushButton.vue";
import { inspectionStatusLabel } from "../inspection";
import type { ResourceKind, ResourceSummary } from "../types";

const props = defineProps<{
  resources: ResourceSummary[];
  pushingId?: string | null;
  pushSupported?: boolean;
  inspectionSupported?: boolean;
  inspectionState?: "idle" | "running" | "completed" | "partial" | "failed" | "timeout";
  inspectionCompleted?: number;
  inspectionTotal?: number;
  inspectionFailed?: number;
  inspectionError?: string | null;
}>();
const emit = defineEmits<{ push: [resource: ResourceSummary]; inspect: [] }>();
const filter = ref<"all" | ResourceKind>("all");
const sort = ref<"comprehensive" | "relevance" | "completeness" | "size" | "seeders">("comprehensive");

const displayedResources = computed(() => {
  const getValue = (resource: ResourceSummary): number | null => {
    if (sort.value === "relevance") return resource.relevance_score ?? null;
    if (sort.value === "completeness") return resource.completeness_score ?? null;
    if (sort.value === "size") return resource.size_bytes;
    return resource.seeders;
  };
  const ordered = sort.value === "comprehensive"
    ? props.resources
    : props.resources
    .map((resource, index) => ({ resource, index, value: getValue(resource) }))
    .sort((left, right) => {
      if (left.value === null && right.value === null) return left.index - right.index;
      if (left.value === null) return 1;
      if (right.value === null) return -1;
      return right.value - left.value || left.index - right.index;
    })
    .map(({ resource }) => resource);
  let magnetCount = 0;
  return ordered.filter((resource) => resource.kind !== "magnet" || ++magnetCount <= 30);
});

const filteredResources = computed(() =>
  filter.value === "all"
    ? displayedResources.value
    : displayedResources.value.filter((resource) => resource.kind === filter.value),
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

function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "未知" : value.toFixed(1);
}

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "未知" : String(value);
}

function inspectionLabel(value: ResourceSummary["inspection_status"]): string {
  if (!value) return "";
  if (value === "running" || value === "queued") return "检测中";
  return inspectionStatusLabel(value);
}

function hasQuality(resource: ResourceSummary): boolean {
  return resource.rank_score !== null && resource.rank_score !== undefined
    || resource.relevance_score !== null && resource.relevance_score !== undefined
    || resource.completeness_score !== null && resource.completeness_score !== undefined;
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
      <div class="resource-controls">
        <div class="segmented" role="group" aria-label="资源类型">
          <button type="button" :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
          <button type="button" :class="{ active: filter === 'magnet' }" @click="filter = 'magnet'">
            <Magnet :size="14" /> 磁力
          </button>
          <button type="button" :class="{ active: filter === '115_share' }" @click="filter = '115_share'">
            <PackageOpen :size="14" /> 115 分享
          </button>
        </div>
        <label class="sort-control">排序
          <select v-model="sort" aria-label="资源排序">
            <option value="comprehensive">综合</option>
            <option value="relevance">相关度</option>
            <option value="completeness">完整度</option>
            <option value="size">大小</option>
            <option value="seeders">做种</option>
          </select>
        </label>
        <button
          v-if="inspectionSupported"
          type="button"
          class="inspection-button"
          :disabled="inspectionState === 'running' || !displayedResources.some((resource) => resource.kind === 'magnet')"
          @click="emit('inspect')"
        >
          <LoaderCircle v-if="inspectionState === 'running'" class="spin" :size="15" />
          <ScanSearch v-else :size="15" />
          {{ inspectionState === 'running' ? '检测中' : '检测本页磁力' }}
        </button>
      </div>
    </div>
    <p v-if="inspectionState === 'running'" class="inspection-progress" role="status">
      正在检测 {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }} 条磁力
    </p>
    <p v-else-if="inspectionState === 'completed'" class="inspection-progress success" role="status">本页磁力检测完成 · 已完成 {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}<span v-if="inspectionFailed"> · 失败 {{ inspectionFailed }} 条</span></p>
    <p v-else-if="inspectionState === 'partial' || inspectionState === 'timeout' || inspectionState === 'failed'" class="inspection-progress warning" role="status">
      {{ inspectionError ?? (inspectionState === 'timeout' ? '检测超时，可重试' : inspectionState === 'partial' ? `部分失败：${inspectionFailed ?? 0} 条磁力检测失败` : '检测失败，可重试') }} · 已完成 {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}
    </p>

    <div v-if="filteredResources.length" class="resource-table-wrap">
      <table class="resource-table">
        <thead>
          <tr><th>资源</th><th>类型</th><th>大小</th><th>做种</th><th>质量 / 检测</th><th>来源 / 抓取时间</th><th /></tr>
        </thead>
        <tbody>
          <tr v-for="resource in filteredResources" :key="resource.resource_id">
            <td class="resource-name">{{ resource.name }}</td>
            <td><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span></td>
            <td class="numeric">{{ formatSize(resource.size_bytes) }}</td>
            <td class="numeric">{{ resource.seeders ?? '未知' }}</td>
            <td class="quality-cell"><div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div><small v-if="resource.inspection_status">{{ inspectionLabel(resource.inspection_status) }} · 视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</small></td>
            <td><strong>{{ resource.source }}</strong><small>{{ formatDate(resource.captured_at) }}</small></td>
            <td class="action-cell"><PushButton :busy="pushingId === resource.resource_id" :disabled="pushSupported === false" @push="emit('push', resource)" /></td>
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
        <div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div>
        <p v-if="resource.inspection_status" class="inspection-details">{{ inspectionLabel(resource.inspection_status) }} · 视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</p>
        <PushButton :busy="pushingId === resource.resource_id" :disabled="pushSupported === false" @push="emit('push', resource)" />
      </article>
    </div>
  </section>
</template>
