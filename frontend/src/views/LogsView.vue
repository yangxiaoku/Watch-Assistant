<script setup lang="ts">
import { AlertTriangle, ChevronDown, FileText, LoaderCircle, RefreshCw } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ApiClient, ApiError } from "../api";
import { formatTimestamp } from "../format";
import { logCategoryLabel, logCategoryOptions, logLevelLabel, logLevelOptions } from "../statusCatalog";
import type { LogCategory, LogEntry, LogLevel, LogsResponse } from "../types";
import { diagnosticCode, diagnosticReference, safeLocalizedCopy } from "../uiSafety";

const props = defineProps<{ api: ApiClient }>();

const logs = ref<LogsResponse | null>(null);
const logItems = ref<LogEntry[]>([]);
const logsLoading = ref(false);
const logsLoaded = ref(false);
const logsError = ref("");
const logCategory = ref<LogCategory | "">("");
const logLevel = ref<LogLevel | "">("");
const logStatus = ref("");
const logEventCode = ref("");
const logRequestId = ref("");
const logCorrelationId = ref("");
const logTaskId = ref("");
const logActorType = ref("");
const logActorId = ref("");
const logResourceType = ref("");
const logResourceId = ref("");
const nextLogCursor = ref<number | null>(null);
const autoRefreshLogs = ref(true);
const showAdvancedFilters = ref(false);
const logsUpdatedAt = ref<string | null>(null);
let logsRequestId = 0;
let logsRefreshTimer: number | null = null;
let mounted = false;
const logLimit = 20;

const statusOptions = [
  { value: "success", label: "成功" },
  { value: "warning", label: "警告" },
  { value: "partial", label: "部分完成" },
  { value: "failed", label: "失败" },
  { value: "uncertain", label: "待确认" },
  { value: "queued", label: "排队中" },
  { value: "cancelled", label: "已取消" },
  { value: "unregistered", label: "未登记" },
];
const actorTypeOptions = [
  { value: "web", label: "Web" },
  { value: "system", label: "系统" },
  { value: "worker", label: "Worker" },
  { value: "agent", label: "Agent" },
  { value: "userscript", label: "用户脚本" },
];

const hasMoreLogs = computed(() => logsLoaded.value && nextLogCursor.value !== null);

async function loadLogs(append = false, preserve = false) {
  const requestId = ++logsRequestId;
  const cursor = append ? nextLogCursor.value : undefined;
  const previousCursor = nextLogCursor.value;
  if (!append && !preserve) {
    logItems.value = [];
    logs.value = null;
    nextLogCursor.value = null;
    logsLoaded.value = false;
  }
  logsLoading.value = true;
  syncLogsRefreshTimer();
  logsError.value = "";
  try {
    const response = await props.api.logs(currentLogFilters(cursor));
    // 组件卸载后返回的响应不再写入状态
    if (!mounted || requestId !== logsRequestId) return;
    const byId = new Map<number, LogEntry>(
      (append || preserve) ? logItems.value.map((item) => [item.id, item]) : [],
    );
    response.items.forEach((item) => byId.set(item.id, item));
    logItems.value = [...byId.values()].sort((a, b) => b.id - a.id);
    logs.value = response;
    nextLogCursor.value = append
      ? response.next_cursor
      : preserve
        ? previousCursor ?? response.next_cursor
        : response.next_cursor;
    logsLoaded.value = true;
    logsUpdatedAt.value = new Date().toISOString();
  } catch (exception) {
    if (mounted && requestId === logsRequestId) logsError.value = exception instanceof ApiError ? exception.message : "日志加载失败，请稍后重试";
  } finally {
    if (mounted && requestId === logsRequestId) {
      logsLoading.value = false;
      syncLogsRefreshTimer();
    }
  }
}

function currentLogFilters(cursor: number | null = null): Parameters<ApiClient["logs"]>[0] {
  const filters: Parameters<ApiClient["logs"]>[0] = {
    limit: logLimit,
    cursor: cursor ?? undefined,
    category: logCategory.value || undefined,
  };
  if (logLevel.value) filters.level = logLevel.value;
  if (logStatus.value) filters.status = logStatus.value;
  if (logEventCode.value.trim()) filters.eventCode = logEventCode.value.trim();
  if (logRequestId.value.trim()) filters.requestId = logRequestId.value.trim();
  if (logCorrelationId.value.trim()) filters.correlationId = logCorrelationId.value.trim();
  if (logTaskId.value.trim()) filters.taskId = logTaskId.value.trim();
  if (logActorType.value) filters.actorType = logActorType.value;
  if (logActorId.value.trim()) filters.actorId = logActorId.value.trim();
  if (logResourceType.value.trim()) filters.resourceType = logResourceType.value.trim();
  if (logResourceId.value.trim()) filters.resourceId = logResourceId.value.trim();
  return filters;
}

function logMessage(item: LogEntry): string {
  return safeLocalizedCopy(item.message_zh, "日志详情不可用。");
}

function refreshLogs() {
  void loadLogs();
}

function autoRefreshLogsIfVisible() {
  if (!autoRefreshLogs.value || document.visibilityState !== "visible" || logsLoading.value) return;
  void loadLogs(false, true);
}

function onVisibilityChange() {
  syncLogsRefreshTimer();
}

function syncLogsRefreshTimer() {
  const shouldRun = mounted && autoRefreshLogs.value && document.visibilityState === "visible" && !logsLoading.value;
  if (!shouldRun) {
    if (logsRefreshTimer !== null) {
      window.clearInterval(logsRefreshTimer);
      logsRefreshTimer = null;
    }
    return;
  }
  if (logsRefreshTimer === null) logsRefreshTimer = window.setInterval(autoRefreshLogsIfVisible, 5000);
}

function changeLogFilter() {
  void loadLogs();
}

function clearLogFilters() {
  logCategory.value = "";
  logLevel.value = "";
  logStatus.value = "";
  logEventCode.value = "";
  logRequestId.value = "";
  logCorrelationId.value = "";
  logTaskId.value = "";
  logActorType.value = "";
  logActorId.value = "";
  logResourceType.value = "";
  logResourceId.value = "";
  void loadLogs();
}

function loadMoreLogs() {
  if (nextLogCursor.value !== null && !logsLoading.value) void loadLogs(true);
}

function logLevelClass(level: LogLevel) {
  return `log-${level.toLowerCase()}`;
}

onMounted(() => {
  mounted = true;
  document.addEventListener("visibilitychange", onVisibilityChange);
  syncLogsRefreshTimer();
  void loadLogs();
});

onBeforeUnmount(() => {
  mounted = false;
  document.removeEventListener("visibilitychange", onVisibilityChange);
  if (logsRefreshTimer !== null) {
    window.clearInterval(logsRefreshTimer);
    logsRefreshTimer = null;
  }
});

watch(autoRefreshLogs, syncLogsRefreshTimer);
</script>

<template>
  <section class="settings-view">
    <header class="settings-heading">
      <div>
        <p class="eyebrow">事件流</p>
        <h1>日志</h1>
        <p>查看应用事件流，按分类与等级筛选；保留策略在「设置 → 日志」中调整。</p>
      </div>
      <div class="settings-section-actions">
        <label class="settings-toggle"><input v-model="autoRefreshLogs" type="checkbox" />自动刷新</label>
        <button class="icon-button" type="button" title="刷新日志" aria-label="刷新日志" :disabled="logsLoading" @click="refreshLogs"><RefreshCw :size="16" :class="{ spin: logsLoading }" /></button>
      </div>
    </header>

    <div class="settings-filter-row">
      <label>分类<select v-model="logCategory" @change="changeLogFilter"><option value="">全部分类</option><option v-for="option in logCategoryOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <label>等级<select v-model="logLevel" @change="changeLogFilter"><option value="">全部等级</option><option v-for="option in logLevelOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <button class="text-button filter-toggle" type="button" :aria-expanded="showAdvancedFilters" @click="showAdvancedFilters = !showAdvancedFilters"><ChevronDown :size="14" :class="{ 'chevron-open': showAdvancedFilters }" />{{ showAdvancedFilters ? '收起高级筛选' : '高级筛选' }}</button>
      <button class="text-button" type="button" @click="clearLogFilters">清除筛选</button>
    </div>
    <div v-if="showAdvancedFilters" class="settings-filter-row settings-filter-row-advanced">
      <label>状态<select v-model="logStatus" @change="changeLogFilter"><option value="">全部状态</option><option v-for="option in statusOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <label>操作者<select v-model="logActorType" @change="changeLogFilter"><option value="">全部操作者</option><option v-for="option in actorTypeOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <label>事件码<input v-model="logEventCode" type="search" maxlength="128" placeholder="例如 task.failed" @keyup.enter="changeLogFilter" /></label>
      <label>请求 ID<input v-model="logRequestId" type="search" maxlength="128" placeholder="筛选请求" @keyup.enter="changeLogFilter" /></label>
      <label>关联 ID<input v-model="logCorrelationId" type="search" maxlength="128" placeholder="筛选关联" @keyup.enter="changeLogFilter" /></label>
      <label>任务 ID<input v-model="logTaskId" type="search" maxlength="128" placeholder="筛选任务" @keyup.enter="changeLogFilter" /></label>
      <label>操作者 ID<input v-model="logActorId" type="search" maxlength="128" placeholder="筛选操作者" @keyup.enter="changeLogFilter" /></label>
      <label>资源类型<input v-model="logResourceType" type="search" maxlength="64" placeholder="例如 library" @keyup.enter="changeLogFilter" /></label>
      <label>资源 ID<input v-model="logResourceId" type="search" maxlength="128" placeholder="筛选资源" @keyup.enter="changeLogFilter" /></label>
    </div>

    <div v-if="logsLoading && !logsLoaded" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载日志</div>
    <div v-else-if="logsError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ logsError }}</span><button class="text-button" type="button" @click="refreshLogs">重试</button></div>
    <div v-else-if="logsLoaded && !logItems.length" class="settings-empty-block"><FileText :size="24" /><strong>暂无日志</strong><span>调整分类或稍后刷新。</span></div>
    <template v-else-if="logsLoaded">
      <div class="settings-log-table-wrap"><table class="settings-log-table"><thead><tr><th>时间</th><th>级别</th><th>分类</th><th>内容</th></tr></thead><tbody><tr v-for="item in logItems" :key="item.id"><td>{{ formatTimestamp(item.timestamp) }}</td><td><span :class="['log-level', logLevelClass(item.level)]">{{ logLevelLabel(item.level) }}</span></td><td>{{ logCategoryLabel(item.category) }}</td><td><strong>{{ item.title_zh || '应用日志' }}</strong><br />{{ logMessage(item) }}<details v-if="item.request_id || item.task_id || item.event_code" class="settings-log-diagnostics"><summary>诊断信息</summary><small v-if="item.event_code">事件码：{{ diagnosticCode(item.event_code) }}</small><small v-if="item.request_id">请求标识：{{ diagnosticReference(item.request_id) }}</small><small v-if="item.task_id">任务标识：{{ diagnosticReference(item.task_id) }}</small></details></td></tr></tbody></table></div>
      <div class="settings-log-list"><article v-for="item in logItems" :key="item.id" class="settings-log-item"><div><span :class="['log-level', logLevelClass(item.level)]">{{ logLevelLabel(item.level) }}</span><time>{{ formatTimestamp(item.timestamp) }}</time></div><strong>{{ logCategoryLabel(item.category) }} · {{ item.title_zh || '应用日志' }}</strong><p>{{ logMessage(item) }}<details v-if="item.request_id || item.task_id || item.event_code" class="settings-log-diagnostics"><summary>诊断信息</summary><small v-if="item.event_code">事件码：{{ diagnosticCode(item.event_code) }}</small><small v-if="item.request_id">请求标识：{{ diagnosticReference(item.request_id) }}</small><small v-if="item.task_id">任务标识：{{ diagnosticReference(item.task_id) }}</small></details></p></article></div>
      <div class="settings-pagination"><span>已加载 {{ logItems.length }} 条</span><button v-if="hasMoreLogs" class="secondary-button" type="button" :disabled="logsLoading" @click="loadMoreLogs"><LoaderCircle v-if="logsLoading" class="spin" :size="15" />加载更多</button></div>
      <p v-if="logsUpdatedAt" class="settings-updated-at">最后更新 {{ formatTimestamp(logsUpdatedAt) }}</p>
    </template>
  </section>
</template>
