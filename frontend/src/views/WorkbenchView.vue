<script setup lang="ts">
import { ArrowRight, ClipboardCheck, Database, ListTodo, Search, Settings2 } from "@lucide/vue";
import { computed, nextTick, ref } from "vue";
import type { Component } from "vue";
import type { CapabilityAvailability } from "../types";
import { capabilityStatusPresentation, type StatusPresentation } from "../statusCatalog";

type WorkbenchDestination = "workflows" | "organization-plans" | "library" | "settings" | "movies";

const props = defineProps<{
  organizationPlanCapability: CapabilityAvailability;
  strmFullCapability: CapabilityAvailability;
  strmIncrementalCapability: CapabilityAvailability;
}>();

const emit = defineEmits<{
  navigate: [view: WorkbenchDestination];
  search: [query: string];
}>();

const searchQuery = ref("");
const searchError = ref("");
const searchInput = ref<HTMLInputElement | null>(null);

interface WorkbenchEntry {
  key: string;
  eyebrow: string;
  title: string;
  description: string;
  status: StatusPresentation;
  icon: Component;
  destination: WorkbenchDestination;
  actionLabel: string;
}

const organizationStatus = computed(() => capabilityStatusPresentation(props.organizationPlanCapability));
const strmStatus = computed<StatusPresentation>(() => {
  if (props.strmFullCapability.enabled || props.strmIncrementalCapability.enabled) {
    return { label: "可用", tone: "success", nextStep: "进入媒体库查看扫描、STRM 清单和操作状态。" };
  }
  if (props.strmFullCapability.reason_code === "capability_unknown" || props.strmIncrementalCapability.reason_code === "capability_unknown") {
    return { label: "状态待确认", tone: "warning", nextStep: "请刷新设置概览后再决定下一步。" };
  }
  return { label: "需配置", tone: "warning", nextStep: "全量和增量 STRM 均未启用，请前往设置查看功能状态。" };
});

const entries = computed<WorkbenchEntry[]>(() => [
  {
    key: "search",
    eyebrow: "资源发现",
    title: "搜索影视资源",
    description: "从影视资料进入资源查询，结果、检测和推送会保留在详情页。",
    status: { label: "可用", tone: "success", nextStep: "可以开始搜索影片或剧集。" },
    icon: Search,
    destination: "movies",
    actionLabel: "浏览影视",
  },
  {
    key: "tasks",
    eyebrow: "进度跟踪",
    title: "任务中心",
    description: "查看搜索、检测、推送、整理和 STRM 的关联阶段。",
    status: { label: "持续更新", tone: "info", nextStep: "进入任务中心查看当前阶段和下一步。" },
    icon: ListTodo,
    destination: "workflows",
    actionLabel: "打开任务中心",
  },
  {
    key: "organization",
    eyebrow: "受控入库",
    title: "整理计划",
    description: props.organizationPlanCapability.enabled ? "查看本地整理预览，确认后进入受控执行。" : "整理计划未就绪，先查看能力状态和配置原因。",
    status: organizationStatus.value,
    icon: ClipboardCheck,
    destination: props.organizationPlanCapability.enabled ? "organization-plans" : "settings",
    actionLabel: props.organizationPlanCapability.enabled ? "打开整理计划" : "查看整理设置",
  },
  {
    key: "strm",
    eyebrow: "媒体输出",
    title: "媒体库与 STRM",
    description: strmStatus.value.label === "可用" ? "从完整扫描进入 STRM 生成、清单和操作状态。" : "STRM 输出未就绪，先确认设置和能力门禁。",
    status: strmStatus.value,
    icon: Database,
    destination: strmStatus.value.label === "可用" ? "library" : "settings",
    actionLabel: strmStatus.value.label === "可用" ? "打开媒体库" : "查看 STRM 设置",
  },
  {
    key: "settings",
    eyebrow: "系统配置",
    title: "设置",
    description: "集中检查搜索来源、115 凭据、资源检测和日志状态。",
    status: { label: "可查看", tone: "info", nextStep: "进入设置查看当前能力状态。" },
    icon: Settings2,
    destination: "settings",
    actionLabel: "打开设置",
  },
]);

function submitSearch(): void {
  const value = searchQuery.value.trim();
  if (!value) {
    searchError.value = "请输入影片名或剧名。";
    void nextTick(() => searchInput.value?.focus());
    return;
  }
  searchError.value = "";
  emit("search", value);
}

function openEntry(entry: WorkbenchEntry): void {
  emit("navigate", entry.destination);
}
</script>

<template>
  <section class="workbench-view" aria-labelledby="workbench-title">
    <header class="workbench-heading">
      <div>
        <p class="eyebrow">观影流程</p>
        <h1 id="workbench-title">观影工作台</h1>
        <p>从发现资源到受控入库，再到 STRM 输出，当前入口和状态集中在这里。</p>
      </div>
      <ArrowRight :size="24" aria-hidden="true" />
    </header>

    <form class="workbench-search" role="search" @submit.prevent="submitSearch">
      <div>
        <label for="workbench-search-input">快速搜索</label>
        <p>输入片名或剧名，直接进入资源发现结果。</p>
      </div>
      <div class="workbench-search-control">
        <Search :size="17" aria-hidden="true" />
        <input id="workbench-search-input" ref="searchInput" v-model="searchQuery" type="search" placeholder="搜索电影或电视剧" aria-describedby="workbench-search-help" />
        <button class="primary-button" type="submit"><Search :size="16" />搜索</button>
      </div>
      <p id="workbench-search-help" class="workbench-search-help" :class="{ 'workbench-search-error': searchError }" :role="searchError ? 'alert' : 'status'">{{ searchError || "搜索结果支持分页，打开影视后可继续检测和推送。" }}</p>
    </form>

    <div class="workbench-entry-grid" aria-label="工作台入口">
      <article v-for="entry in entries" :key="entry.key" class="workbench-entry">
        <header class="workbench-entry-heading">
          <span class="workbench-entry-icon"><component :is="entry.icon" :size="18" aria-hidden="true" /></span>
          <span class="workbench-entry-status" :class="`status-chip-${entry.status.tone}`">{{ entry.status.label }}</span>
        </header>
        <p class="eyebrow">{{ entry.eyebrow }}</p>
        <h2>{{ entry.title }}</h2>
        <p>{{ entry.description }}</p>
        <small>{{ entry.status.nextStep }}</small>
        <button class="secondary-button workbench-entry-action" type="button" @click="openEntry(entry)"><component :is="entry.icon" :size="15" />{{ entry.actionLabel }}<ArrowRight :size="15" /></button>
      </article>
    </div>

    <p class="workbench-safety-note">整理、STRM 和 115 相关写操作仍按预览、确认、幂等和状态核对流程执行。</p>
  </section>
</template>
