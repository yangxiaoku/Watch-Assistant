<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Magnet, PackageOpen, ScanSearch, Search } from "@lucide/vue";

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
  inspectionMoreAvailable?: boolean;
}>();
const emit = defineEmits<{ push: [resource: ResourceSummary]; inspectMore: [] }>();
const filter = ref<"all" | ResourceKind>("all");
type ResourceSort = "comprehensive" | "relevance" | "completeness" | "size" | "seeders";
type ResourceTag = "all" | "4k" | "1080p" | "720p" | "subtitle";

const RESOURCE_SORT_KEY = "watch-assistant:resource-sort";
const SORT_OPTIONS: ResourceSort[] = ["comprehensive", "relevance", "completeness", "size", "seeders"];
const inferredTags: Array<{ key: ResourceTag; label: string }> = [
  { key: "all", label: "全部" },
  { key: "4k", label: "4K/2160P" },
  { key: "1080p", label: "1080P" },
  { key: "720p", label: "720P" },
  { key: "subtitle", label: "字幕" },
];

function readStoredSort(): ResourceSort {
  if (typeof window === "undefined") return "comprehensive";
  const value = window.localStorage.getItem(RESOURCE_SORT_KEY);
  return SORT_OPTIONS.includes(value as ResourceSort) ? value as ResourceSort : "comprehensive";
}

const sort = ref<ResourceSort>(readStoredSort());
const resourceSearch = ref("");
const inferredFilter = ref<ResourceTag>("all");

watch(sort, (value) => {
  if (typeof window !== "undefined") window.localStorage.setItem(RESOURCE_SORT_KEY, value);
});

const orderedResources = computed(() => {
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

const kindCounts = computed(() => ({
  all: orderedResources.value.length,
  magnet: orderedResources.value.filter((resource) => resource.kind === "magnet").length,
  share: orderedResources.value.filter((resource) => resource.kind === "115_share").length,
}));

function hasInferredTag(resource: ResourceSummary, tag: Exclude<ResourceTag, "all">): boolean {
  const name = resource.name.toLowerCase();
  if (tag === "4k") return /(?:4k|2160p|uhd)/i.test(name);
  if (tag === "1080p") return /1080p/i.test(name);
  if (tag === "720p") return /720p/i.test(name);
  return /字幕|sub(?:title)?|chs|cht|简中|繁中|双语/i.test(name);
}

const tagCounts = computed(() => ({
  all: orderedResources.value.length,
  "4k": orderedResources.value.filter((resource) => hasInferredTag(resource, "4k")).length,
  "1080p": orderedResources.value.filter((resource) => hasInferredTag(resource, "1080p")).length,
  "720p": orderedResources.value.filter((resource) => hasInferredTag(resource, "720p")).length,
  subtitle: orderedResources.value.filter((resource) => hasInferredTag(resource, "subtitle")).length,
}));

const filteredResources = computed(() => {
  const query = resourceSearch.value.trim().toLowerCase();
  return orderedResources.value.filter((resource) => {
    if (filter.value !== "all" && resource.kind !== filter.value) return false;
    if (query && !resource.name.toLowerCase().includes(query)) return false;
    if (inferredFilter.value !== "all" && !hasInferredTag(resource, inferredFilter.value)) return false;
    return true;
  });
});

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

function sizeSourceLabel(resource: ResourceSummary): string {
  if (resource.size_source === "inspection") return "已验证";
  if (resource.size_source === "pansou") return "来源";
  return "";
}

function seedersSourceLabel(resource: ResourceSummary): string {
  return resource.seeders_source === "pansou" ? "来源数据" : "";
}

function seedersTitle(resource: ResourceSummary): string | undefined {
  return resource.seeders_source === "pansou" && resource.seeders_observed_at
    ? `做种数据采集于 ${formatDate(resource.seeders_observed_at)}`
    : undefined;
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
      <div class="resource-heading-copy">
        <p class="eyebrow">PanSou 聚合</p>
        <h2>可用资源 <span>{{ filteredResources.length }}<small v-if="filteredResources.length !== orderedResources.length"> / {{ orderedResources.length }}</small></span></h2>
        <p v-if="inspectionState === 'running'" class="inspection-progress" role="status">检测中 {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }} 条磁力<span v-if="inspectionFailed"> · 失败 {{ inspectionFailed }} 条</span></p>
        <p v-else-if="inspectionState === 'completed'" class="inspection-progress success" role="status">检测完成 · {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}<span v-if="inspectionFailed"> · 失败 {{ inspectionFailed }} 条</span></p>
        <p v-else-if="inspectionState === 'partial' || inspectionState === 'timeout' || inspectionState === 'failed'" class="inspection-progress warning" role="status">{{ inspectionError ?? (inspectionState === 'timeout' ? '检测超时，可重试' : inspectionState === 'partial' ? `部分失败：${inspectionFailed ?? 0} 条磁力检测失败` : '检测失败，可重试') }} · {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}</p>
      </div>
      <div class="resource-controls">
        <div class="segmented" role="group" aria-label="资源类型">
          <button type="button" :class="{ active: filter === 'all' }" @click="filter = 'all'">全部 <span>{{ kindCounts.all }}</span></button>
          <button type="button" :class="{ active: filter === 'magnet' }" @click="filter = 'magnet'">
            <Magnet :size="14" />磁力 <span>{{ kindCounts.magnet }}</span>
          </button>
          <button type="button" :class="{ active: filter === '115_share' }" @click="filter = '115_share'">
            <PackageOpen :size="14" />115 分享 <span>{{ kindCounts.share }}</span>
          </button>
        </div>
        <label class="resource-name-search"><Search :size="15" /><span class="sr-only">搜索资源名称</span><input v-model="resourceSearch" type="search" aria-label="资源名称搜索" placeholder="搜索资源名称" /></label>
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
          v-if="inspectionSupported && inspectionMoreAvailable && inspectionState !== 'running'"
          type="button"
          class="secondary-button inspection-more-button"
          @click="emit('inspectMore')"
        >
          <ScanSearch :size="15" />检测更多
        </button>
      </div>
    </div>
    <div class="resource-filter-bar" role="group" aria-label="资源名称标签">
      <button v-for="tag in inferredTags" :key="tag.key" type="button" :class="{ active: inferredFilter === tag.key }" :aria-pressed="inferredFilter === tag.key" @click="inferredFilter = tag.key">{{ tag.label }} <span>{{ tagCounts[tag.key] }}</span></button>
    </div>

    <div v-if="filteredResources.length" class="resource-table-wrap">
      <table class="resource-table">
        <thead>
          <tr><th>资源名称</th><th>质量</th><th>大小</th><th>做种</th><th>内容检测</th><th>来源 / 时间</th><th>推送</th></tr>
        </thead>
        <tbody>
          <tr v-for="resource in filteredResources" :key="resource.resource_id">
            <td class="resource-name"><div class="resource-name-meta"><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span></div><span class="resource-title">{{ resource.name }}</span></td>
            <td class="quality-cell"><div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div></td>
            <td class="numeric"><span>{{ formatSize(resource.size_bytes) }}</span><small v-if="sizeSourceLabel(resource)">{{ sizeSourceLabel(resource) }}</small></td>
            <td class="numeric" :title="seedersTitle(resource)"><span>{{ resource.seeders ?? '未知' }}</span><small v-if="seedersSourceLabel(resource)">{{ seedersSourceLabel(resource) }}</small></td>
            <td class="inspection-cell"><template v-if="resource.inspection_status"><strong>{{ inspectionLabel(resource.inspection_status) }}</strong><small>视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</small></template><span v-else class="inspection-empty">未检测</span></td>
            <td class="source-cell"><strong>{{ resource.source }}</strong><small>{{ formatDate(resource.captured_at) }}</small></td>
            <td class="action-cell"><PushButton :busy="pushingId === resource.resource_id" :disabled="pushSupported === false" @push="emit('push', resource)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty-state">当前筛选没有资源</div>

    <div class="resource-cards">
      <article v-for="resource in filteredResources" :key="resource.resource_id" class="resource-card">
        <div class="card-heading"><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span><span>{{ resource.source }}</span></div>
        <h3 class="resource-card-title">{{ resource.name }}</h3>
        <dl><div><dt>大小</dt><dd>{{ formatSize(resource.size_bytes) }}<small v-if="sizeSourceLabel(resource)" class="metric-source">{{ sizeSourceLabel(resource) }}</small></dd></div><div><dt>做种</dt><dd>{{ resource.seeders ?? '未知' }}<small v-if="seedersSourceLabel(resource)" class="metric-source">{{ seedersSourceLabel(resource) }}</small></dd></div><div><dt>抓取</dt><dd>{{ formatDate(resource.captured_at) }}</dd></div></dl>
        <div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div>
        <p v-if="resource.inspection_status" class="inspection-details">{{ inspectionLabel(resource.inspection_status) }} · 视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</p><p v-else class="inspection-details inspection-empty">未检测</p>
        <PushButton :busy="pushingId === resource.resource_id" :disabled="pushSupported === false" @push="emit('push', resource)" />
      </article>
    </div>
  </section>
</template>
