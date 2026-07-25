<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ChevronFirst, ChevronLast, ChevronLeft, ChevronRight, LoaderCircle, Magnet, PackageOpen, ScanSearch, Search } from "@lucide/vue";

import PushButton from "./PushButton.vue";
import { inspectionStatusLabel } from "../inspection";
import { canPushResource, NO_PUSH_CAPABILITIES, type PushCapabilities } from "../push";
import type { ResourceFacets, ResourceKind, ResourceQuality, ResourceSort, ResourceSummary } from "../types";

const props = defineProps<{
  resources: ResourceSummary[];
  facets?: ResourceFacets;
  total?: number;
  page?: number;
  pageSize?: 25 | 50 | 100;
  totalPages?: number;
  resourceKind?: "all" | ResourceKind;
  resourceQuality?: "all" | ResourceQuality;
  resourceQuery?: string;
  resourceSort?: ResourceSort;
  resourceLoading?: boolean;
  resourceError?: string;
  paginationUnavailable?: boolean;
  pushingId?: string | null;
  pushCapabilities?: PushCapabilities;
  inspectionSupported?: boolean;
  inspectionState?: "idle" | "running" | "completed" | "partial" | "failed" | "timeout";
  inspectionCompleted?: number;
  inspectionTotal?: number;
  inspectionFailed?: number;
  inspectionError?: string | null;
  inspectionMoreAvailable?: boolean;
  inspectionRetryAvailable?: boolean;
}>();

const emit = defineEmits<{
  push: [resource: ResourceSummary];
  inspectMore: [];
  retryFailed: [];
  retryPage: [];
  page: [page: number];
  kind: [kind: "all" | ResourceKind];
  quality: [quality: "all" | ResourceQuality];
  query: [query: string];
  sort: [sort: ResourceSort];
  pageSize: [pageSize: 25 | 50 | 100];
}>();

const queryDraft = ref(props.resourceQuery ?? "");
watch(() => props.resourceQuery, (value) => { queryDraft.value = value ?? ""; });
watch(() => props.resourceError, () => { queryDraft.value = props.resourceQuery ?? ""; });

const facets = computed(() => props.facets ?? { magnet: 0, share: 0, "4k": 0, "1080p": 0, "720p": 0, subtitle: 0 });
const total = computed(() => props.total ?? props.resources.length);
const page = computed(() => props.page ?? 1);
const pageSize = computed(() => props.pageSize ?? 25);
const totalPages = computed(() => Math.max(1, props.totalPages ?? 1));
const kind = computed(() => props.resourceKind ?? "all");
const quality = computed(() => props.resourceQuality ?? "all");
const sort = computed(() => props.resourceSort ?? "comprehensive");
const activePushCapabilities = computed(() => props.pushCapabilities ?? NO_PUSH_CAPABILITIES);

const kindCounts = computed(() => ({ all: total.value, magnet: facets.value.magnet, share: facets.value.share }));
const qualityCounts = computed(() => ({
  all: total.value,
  "4k": facets.value["4k"],
  "1080p": facets.value["1080p"],
  "720p": facets.value["720p"],
  subtitle: facets.value.subtitle,
}));

function formatSize(value: number | null): string {
  if (value === null) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
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
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function canPush(resource: ResourceSummary): boolean { return canPushResource(resource, activePushCapabilities.value); }
function pushTitle(resource: ResourceSummary): string {
  if (resource.kind === "115_share" && !canPush(resource)) return "115 分享转存尚未验证";
  if (!canPush(resource)) return "磁力云下载不可用";
  return "推送到 115";
}
function pageRequest(target: number) {
  if (props.resourceLoading) return;
  const safeTarget = Math.max(1, Math.min(totalPages.value, target));
  if (safeTarget !== page.value) emit("page", safeTarget);
}
</script>

<template>
  <section class="resource-surface" :aria-busy="resourceLoading ? 'true' : 'false'">
    <div class="resource-toolbar">
      <div class="resource-heading-copy">
        <p class="eyebrow">资源分页</p>
        <h2>可用资源 <span>{{ total }}</span></h2>
        <p v-if="inspectionState === 'running'" class="inspection-progress" role="status">检测中 {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }} 条磁力<span v-if="inspectionFailed"> · 失败 {{ inspectionFailed }} 条</span></p>
        <p v-else-if="inspectionState === 'completed'" class="inspection-progress success" role="status">检测完成 · {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}<span v-if="inspectionFailed"> · 失败 {{ inspectionFailed }} 条</span></p>
        <p v-else-if="inspectionState === 'partial' || inspectionState === 'timeout' || inspectionState === 'failed'" class="inspection-progress warning" role="status">{{ inspectionError ?? (inspectionState === 'timeout' ? '检测超时，可重试' : inspectionState === 'partial' ? `部分失败：${inspectionFailed ?? 0} 条磁力检测失败` : '检测失败，可重试') }} · {{ inspectionCompleted ?? 0 }} / {{ inspectionTotal ?? 0 }}</p>
      </div>
      <div class="resource-controls">
        <div class="segmented" role="group" aria-label="资源类型">
          <button type="button" :class="{ active: kind === 'all' }" @click="emit('kind', 'all')">全部 <span>{{ kindCounts.all }}</span></button>
          <button type="button" :class="{ active: kind === 'magnet' }" @click="emit('kind', 'magnet')"><Magnet :size="14" />磁力 <span>{{ kindCounts.magnet }}</span></button>
          <button type="button" :class="{ active: kind === '115_share' }" @click="emit('kind', '115_share')"><PackageOpen :size="14" />115 分享 <span>{{ kindCounts.share }}</span></button>
        </div>
        <label class="resource-name-search"><Search :size="15" /><span class="sr-only">搜索资源名称</span><input :value="queryDraft" type="search" aria-label="资源名称搜索" placeholder="搜索资源名称" @input="emit('query', ($event.target as HTMLInputElement).value)" /></label>
        <label class="sort-control">排序<select :value="sort" aria-label="资源排序" @change="emit('sort', ($event.target as HTMLSelectElement).value as ResourceSort)"><option value="comprehensive">综合</option><option value="relevance">相关度</option><option value="completeness">完整度</option><option value="size">大小</option><option value="seeders">做种</option></select></label>
        <label class="sort-control">每页<select :value="pageSize" aria-label="资源每页数量" @change="emit('pageSize', Number(($event.target as HTMLSelectElement).value) as 25 | 50 | 100)"><option :value="25">25</option><option :value="50">50</option><option :value="100">100</option></select></label>
        <button v-if="inspectionSupported && inspectionMoreAvailable && inspectionState !== 'running'" type="button" class="secondary-button inspection-more-button" @click="emit('inspectMore')"><ScanSearch :size="15" />检测更多</button>
        <button v-if="inspectionSupported && inspectionRetryAvailable && inspectionState !== 'running'" type="button" class="secondary-button inspection-more-button" @click="emit('retryFailed')"><ScanSearch :size="15" />重试失败项</button>
      </div>
    </div>
    <div class="resource-filter-bar" role="group" aria-label="资源质量筛选">
      <button v-for="tag in [{ key: 'all', label: '全部' }, { key: '4k', label: '4K/2160P' }, { key: '1080p', label: '1080P' }, { key: '720p', label: '720P' }, { key: 'subtitle', label: '字幕' }]" :key="tag.key" type="button" :class="{ active: quality === tag.key }" @click="emit('quality', tag.key as 'all' | ResourceQuality)">{{ tag.label }} <span>{{ qualityCounts[tag.key as keyof typeof qualityCounts] }}</span></button>
    </div>
    <p v-if="paginationUnavailable" class="resource-page-notice" role="status">{{ resourceError }}</p>
    <p v-else-if="resourceError" class="resource-page-error" role="alert">{{ resourceError }} <button class="text-button" type="button" @click="emit('retryPage')">重试</button></p>
    <div v-if="resources.length" class="resource-table-wrap" :class="{ 'resource-list-loading': resourceLoading }">
      <table class="resource-table">
        <thead><tr><th>资源名称</th><th>质量</th><th>大小</th><th>做种</th><th>内容检测</th><th>来源 / 时间</th><th>推送</th></tr></thead>
        <tbody>
          <tr v-for="resource in resources" :key="resource.resource_id">
            <td class="resource-name"><div class="resource-name-meta"><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span></div><span class="resource-title">{{ resource.name }}</span></td>
            <td class="quality-cell"><div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div></td>
            <td class="numeric"><span>{{ formatSize(resource.size_bytes) }}</span><small v-if="sizeSourceLabel(resource)">{{ sizeSourceLabel(resource) }}</small></td>
            <td class="numeric" :title="seedersTitle(resource)"><span>{{ resource.seeders ?? '未知' }}</span><small v-if="resource.seeders_source === 'pansou'">来源数据</small></td>
            <td class="inspection-cell"><template v-if="resource.inspection_status"><strong>{{ inspectionLabel(resource.inspection_status) }}</strong><small>视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</small></template><span v-else class="inspection-empty">未检测</span></td>
            <td class="source-cell"><strong>{{ resource.source }}</strong><small>{{ formatDate(resource.captured_at) }}</small></td>
            <td class="action-cell"><PushButton :busy="pushingId === resource.resource_id" :disabled="!canPush(resource)" :title="pushTitle(resource)" @push="emit('push', resource)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="resources.length" class="resource-cards" :class="{ 'resource-list-loading': resourceLoading }">
      <article v-for="resource in resources" :key="resource.resource_id" class="resource-card">
        <div class="card-heading"><span class="kind-label">{{ resource.kind === 'magnet' ? '磁力' : '115 分享' }}</span><span>{{ resource.source }}</span></div>
        <h3 class="resource-card-title">{{ resource.name }}</h3>
        <dl><div><dt>大小</dt><dd>{{ formatSize(resource.size_bytes) }}<small v-if="sizeSourceLabel(resource)" class="metric-source">{{ sizeSourceLabel(resource) }}</small></dd></div><div><dt>做种</dt><dd>{{ resource.seeders ?? '未知' }}<small v-if="resource.seeders_source === 'pansou'" class="metric-source">来源数据</small></dd></div><div><dt>抓取</dt><dd>{{ formatDate(resource.captured_at) }}</dd></div></dl>
        <div v-if="hasQuality(resource)" class="quality-metrics"><span>综合 {{ formatScore(resource.rank_score) }}</span><span>相关 {{ formatScore(resource.relevance_score) }}</span><span>完整 {{ formatScore(resource.completeness_score) }}</span></div>
        <p v-if="resource.inspection_status" class="inspection-details">{{ inspectionLabel(resource.inspection_status) }} · 视频 {{ formatCount(resource.video_file_count) }} · 字幕 {{ formatCount(resource.subtitle_count) }} · 样本 {{ formatCount(resource.sample_count) }}</p><p v-else class="inspection-details inspection-empty">未检测</p>
        <PushButton :busy="pushingId === resource.resource_id" :disabled="!canPush(resource)" :title="pushTitle(resource)" @push="emit('push', resource)" />
      </article>
    </div>
    <div v-else-if="resourceLoading" class="resource-loading-local" role="status"><LoaderCircle class="spin" :size="18" />正在加载资源分页</div>
    <div v-else class="empty-state">当前没有资源</div>
    <nav v-if="!paginationUnavailable && totalPages > 1" class="resource-pagination" aria-label="资源分页">
      <span>第 {{ page }} / {{ totalPages }} 页</span>
      <div><button type="button" aria-label="第一页" :disabled="resourceLoading || page <= 1" @click="pageRequest(1)"><ChevronFirst :size="15" /></button><button type="button" aria-label="上一页" :disabled="resourceLoading || page <= 1" @click="pageRequest(page - 1)"><ChevronLeft :size="15" /></button><button type="button" aria-label="下一页" :disabled="resourceLoading || page >= totalPages" @click="pageRequest(page + 1)"><ChevronRight :size="15" /></button><button type="button" aria-label="最后一页" :disabled="resourceLoading || page >= totalPages" @click="pageRequest(totalPages)"><ChevronLast :size="15" /></button></div>
    </nav>
  </section>
</template>
