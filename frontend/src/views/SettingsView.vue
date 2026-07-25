<script setup lang="ts">
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cookie,
  Database,
  FileText,
  LoaderCircle,
  RefreshCw,
  Save,
  Server,
  Settings2,
  ShieldCheck,
  XCircle,
} from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import type {
  LogEntry,
  LogLevel,
  LoggingSettingsResponse,
  LogsResponse,
  P115SettingsResponse,
  SettingsComponentStatus,
  SettingsOverviewResponse,
} from "../types";

const props = defineProps<{ api: ApiClient }>();

type SettingsSection = "overview" | "logs" | "p115";

const sections = [
  { id: "overview" as const, label: "概览", icon: Activity },
  { id: "logs" as const, label: "日志", icon: FileText },
  { id: "p115" as const, label: "115 推送", icon: ShieldCheck },
];
const levelOptions: Array<{ value: LogLevel; label: string }> = [
  { value: "debug", label: "调试" },
  { value: "info", label: "信息" },
  { value: "warning", label: "警告" },
  { value: "error", label: "错误" },
];
const categoryOptions = [
  ["", "全部分类"],
  ["system", "系统"],
  ["auth", "认证"],
  ["search", "搜索"],
  ["inspection", "检测"],
  ["push", "推送"],
] as const;

const activeSection = ref<SettingsSection>("overview");
const overview = ref<SettingsOverviewResponse | null>(null);
const overviewLoading = ref(true);
const overviewError = ref("");
const p115 = ref<P115SettingsResponse | null>(null);
const p115Loading = ref(true);
const p115Error = ref("");
const validationState = ref<"idle" | "running" | "success" | "error">("idle");
const validationMessage = ref("");

const logging = ref<LoggingSettingsResponse | null>(null);
const loggingLoading = ref(true);
const loggingError = ref("");
const draftLevel = ref<LogLevel>("info");
const draftRetentionDays = ref(30);
const draftCapacityMb = ref(512);
const savingLogging = ref(false);
const saveError = ref("");
const conflict = ref(false);

const logs = ref<LogsResponse | null>(null);
const logItems = ref<LogEntry[]>([]);
const logsLoading = ref(false);
const logsLoaded = ref(false);
const logsError = ref("");
const logLevel = ref<LogLevel | "">("");
const logCategory = ref("");
const logPage = ref(1);
const logPageSize = 20;

const loggingDirty = computed(() => {
  if (!logging.value) return false;
  return logging.value.level !== draftLevel.value
    || logging.value.retention_days !== draftRetentionDays.value
    || logging.value.capacity_mb !== draftCapacityMb.value;
});
const currentSection = computed(() => sections.find((section) => section.id === activeSection.value));
const hasPreviousLogPage = computed(() => (logs.value?.page ?? 1) > 1);
const hasNextLogPage = computed(() => (logs.value?.page ?? 1) < (logs.value?.total_pages ?? 1));

function selectSection(section: SettingsSection) {
  activeSection.value = section;
  if (section === "logs" && !logsLoaded.value) void loadLogs();
}

function selectMobileSection(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (value === "overview" || value === "logs" || value === "p115") selectSection(value);
}

async function loadOverview() {
  overviewLoading.value = true;
  overviewError.value = "";
  try {
    overview.value = await props.api.settingsOverview();
  } catch (exception) {
    overviewError.value = exception instanceof ApiError ? exception.message : "概览加载失败，请稍后重试";
  } finally {
    overviewLoading.value = false;
  }
}

async function loadP115() {
  p115Loading.value = true;
  p115Error.value = "";
  try {
    p115.value = await props.api.p115Settings();
  } catch (exception) {
    p115Error.value = exception instanceof ApiError ? exception.message : "115 状态加载失败，请稍后重试";
  } finally {
    p115Loading.value = false;
  }
}

function applyLogging(value: LoggingSettingsResponse) {
  logging.value = value;
  draftLevel.value = value.level;
  draftRetentionDays.value = value.retention_days;
  draftCapacityMb.value = value.capacity_mb;
}

async function loadLogging() {
  loggingLoading.value = true;
  loggingError.value = "";
  conflict.value = false;
  try {
    applyLogging(await props.api.loggingSettings());
  } catch (exception) {
    loggingError.value = exception instanceof ApiError ? exception.message : "日志设置加载失败，请稍后重试";
  } finally {
    loggingLoading.value = false;
  }
}

async function saveLogging() {
  if (!logging.value || savingLogging.value || !loggingDirty.value) return;
  savingLogging.value = true;
  saveError.value = "";
  conflict.value = false;
  try {
    applyLogging(await props.api.updateLoggingSettings({
      revision: logging.value.revision,
      level: draftLevel.value,
      retention_days: draftRetentionDays.value,
      capacity_mb: draftCapacityMb.value,
    }));
  } catch (exception) {
    if (exception instanceof ApiError && exception.status === 409) {
      conflict.value = true;
      saveError.value = "设置已被其他请求修改，请重新加载后再保存。";
    } else {
      saveError.value = exception instanceof ApiError ? exception.message : "保存失败，请稍后重试";
    }
  } finally {
    savingLogging.value = false;
  }
}

async function loadLogs() {
  logsLoading.value = true;
  logsError.value = "";
  try {
    const response = await props.api.logs({
      level: logLevel.value || undefined,
      category: logCategory.value || undefined,
      page: logPage.value,
      pageSize: logPageSize,
    });
    logs.value = response;
    logItems.value = response.items;
    logPage.value = response.page;
    logsLoaded.value = true;
  } catch (exception) {
    logsError.value = exception instanceof ApiError ? exception.message : "日志加载失败，请稍后重试";
  } finally {
    logsLoading.value = false;
  }
}

function refreshLogs() {
  void loadLogs();
}

function changeLogFilter() {
  logPage.value = 1;
  void loadLogs();
}

function changeLogPage(page: number) {
  if (page < 1 || page > (logs.value?.total_pages ?? 1)) return;
  logPage.value = page;
  void loadLogs();
}

async function validateP115() {
  if (validationState.value === "running") return;
  validationState.value = "running";
  validationMessage.value = "正在验证已配置 Cookie";
  try {
    p115.value = await props.api.validateP115Cookie();
    validationState.value = "success";
    validationMessage.value = "Cookie 验证完成";
  } catch (exception) {
    validationState.value = "error";
    validationMessage.value = exception instanceof ApiError ? exception.message : "Cookie 验证失败，请稍后重试";
  }
}

function formatBytes(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "未知";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatUptime(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "未知";
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return days ? `${days} 天 ${hours} 小时` : `${hours} 小时 ${minutes} 分钟`;
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString("zh-CN", { hour12: false });
}

function componentLabel(status: SettingsComponentStatus) {
  return { ok: "正常", degraded: "降级", down: "异常", unknown: "未知" }[status];
}

function componentIcon(status: SettingsComponentStatus) {
  return status === "ok" ? CheckCircle2 : status === "down" ? XCircle : AlertTriangle;
}

function levelLabel(level: LogLevel) {
  return levelOptions.find((option) => option.value === level)?.label ?? level;
}

function p115ReadinessLabel(value: P115SettingsResponse["readiness"]) {
  return { ready: "已就绪", not_ready: "未就绪", unknown: "未知" }[value];
}

function cookieSourceLabel(value: P115SettingsResponse["cookie_source"]) {
  return { file: "文件", environment: "环境变量", unknown: "未知" }[value];
}

function cookieStructureLabel(value: P115SettingsResponse["cookie_structure"]) {
  return { valid: "结构正常", invalid: "结构异常", unknown: "未知" }[value];
}

onMounted(() => {
  void loadOverview();
  void loadLogging();
  void loadP115();
});
</script>

<template>
  <section class="settings-view">
    <header class="settings-heading">
      <div>
        <p class="eyebrow">WORKSPACE SETTINGS</p>
        <h1>设置</h1>
        <p>查看运行状态、日志策略和 115 推送准备情况。</p>
      </div>
      <Settings2 :size="28" />
    </header>

    <div class="settings-layout">
      <aside class="settings-nav" aria-label="设置分区">
        <button v-for="item in sections" :key="item.id" type="button" :class="{ active: activeSection === item.id }" @click="selectSection(item.id)">
          <component :is="item.icon" :size="16" />{{ item.label }}
        </button>
      </aside>

      <div class="settings-content">
        <label class="settings-mobile-select">分区<select aria-label="设置分区" :value="activeSection" @change="selectMobileSection"><option v-for="item in sections" :key="item.id" :value="item.id">{{ item.label }}</option></select></label>
        <div class="settings-section-kicker"><span>{{ currentSection?.label }}</span><span>WATCH ASSISTANT</span></div>

        <section v-if="activeSection === 'overview'" class="settings-section" aria-labelledby="overview-title">
          <header class="settings-section-heading"><div><p class="eyebrow">SYSTEM SNAPSHOT</p><h2 id="overview-title">概览</h2></div><button class="icon-button" type="button" title="刷新概览" aria-label="刷新概览" :disabled="overviewLoading" @click="loadOverview"><RefreshCw :size="16" :class="{ spin: overviewLoading }" /></button></header>
          <div v-if="overviewLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载概览</div>
          <div v-else-if="overviewError" class="settings-state settings-state-error"><AlertTriangle :size="18" /><span>{{ overviewError }}</span><button class="text-button" type="button" @click="loadOverview">重试</button></div>
          <template v-else-if="overview">
            <div class="settings-metrics">
              <div class="settings-metric"><span>版本</span><strong>{{ overview.version || '未知' }}</strong></div>
              <div class="settings-metric"><span>运行时间</span><strong>{{ formatUptime(overview.uptime_seconds) }}</strong></div>
              <div class="settings-metric"><span>数据库大小</span><strong>{{ formatBytes(overview.database_size_bytes) }}</strong></div>
              <div class="settings-metric"><span>配置版本</span><strong class="settings-metric-mono">{{ overview.revision }}</strong></div>
            </div>
            <div class="settings-subsection"><h3>组件状态</h3><div class="settings-status-list"><div v-for="component in overview.components" :key="component.name" class="settings-status-row"><span class="settings-status-name"><component :is="componentIcon(component.status)" :class="`status-${component.status}`" :size="16" />{{ component.name }}</span><span :class="`settings-status-value status-${component.status}`">{{ componentLabel(component.status) }}<small v-if="component.detail">{{ component.detail }}</small></span></div><div v-if="!overview.components.length" class="settings-empty">暂无组件状态</div></div></div>
          </template>
        </section>

        <section v-else-if="activeSection === 'logs'" class="settings-section" aria-labelledby="logs-title">
          <header class="settings-section-heading"><div><p class="eyebrow">EVENT STREAM</p><h2 id="logs-title">日志</h2></div><button class="icon-button" type="button" title="刷新日志" aria-label="刷新日志" :disabled="logsLoading" @click="refreshLogs"><RefreshCw :size="16" :class="{ spin: logsLoading }" /></button></header>
          <div class="settings-filter-row"><label>级别<select v-model="logLevel" @change="changeLogFilter"><option value="">全部级别</option><option v-for="option in levelOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><label>分类<select v-model="logCategory" @change="changeLogFilter"><option v-for="option in categoryOptions" :key="option[0]" :value="option[0]">{{ option[1] }}</option></select></label></div>
          <div v-if="logsLoading && !logsLoaded" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载日志</div>
          <div v-else-if="logsError" class="settings-state settings-state-error"><AlertTriangle :size="18" /><span>{{ logsError }}</span><button class="text-button" type="button" @click="refreshLogs">重试</button></div>
          <div v-else-if="logsLoaded && !logItems.length" class="settings-empty-block"><FileText :size="24" /><strong>暂无日志</strong><span>调整筛选条件或稍后刷新。</span></div>
          <template v-else-if="logsLoaded">
            <div class="settings-log-table-wrap"><table class="settings-log-table"><thead><tr><th>时间</th><th>级别</th><th>分类</th><th>内容</th></tr></thead><tbody><tr v-for="item in logItems" :key="item.id"><td>{{ formatTimestamp(item.timestamp) }}</td><td><span :class="`log-level log-${item.level}`">{{ levelLabel(item.level) }}</span></td><td>{{ item.category }}</td><td>{{ item.message }}</td></tr></tbody></table></div>
            <div class="settings-log-list"><article v-for="item in logItems" :key="item.id" class="settings-log-item"><div><span :class="`log-level log-${item.level}`">{{ levelLabel(item.level) }}</span><time>{{ formatTimestamp(item.timestamp) }}</time></div><strong>{{ item.category }}</strong><p>{{ item.message }}</p></article></div>
            <div class="settings-pagination"><span>共 {{ logs.total }} 条，第 {{ logs.page }} / {{ logs.total_pages }} 页</span><div><button class="icon-button" type="button" title="上一页" aria-label="上一页" :disabled="!hasPreviousLogPage || logsLoading" @click="changeLogPage(logs.page - 1)"><ChevronLeft :size="16" /></button><button class="icon-button" type="button" title="下一页" aria-label="下一页" :disabled="!hasNextLogPage || logsLoading" @click="changeLogPage(logs.page + 1)"><ChevronRight :size="16" /></button></div></div>
          </template>
          <div class="settings-subsection logging-settings"><h3>日志保留</h3><div v-if="loggingLoading" class="settings-loading"><LoaderCircle class="spin" :size="18" />正在加载日志设置</div><div v-else-if="loggingError" class="settings-state settings-state-error"><AlertTriangle :size="18" /><span>{{ loggingError }}</span><button class="text-button" type="button" @click="loadLogging">重试</button></div><template v-else-if="logging"><div class="settings-form-grid"><label>最低级别<select v-model="draftLevel"><option v-for="option in levelOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><label>保留天数<input v-model.number="draftRetentionDays" type="number" min="1" max="3650" /></label><label>容量上限（MB）<input v-model.number="draftCapacityMb" type="number" min="16" max="102400" /></label></div><div v-if="loggingDirty" class="settings-save-bar"><span>有未保存的日志设置</span><div><button class="secondary-button" type="button" :disabled="savingLogging" @click="loadLogging">取消</button><button class="primary-button" type="button" :disabled="savingLogging" @click="saveLogging"><LoaderCircle v-if="savingLogging" class="spin" :size="15" /><Save v-else :size="15" />保存</button></div></div><div v-if="saveError" class="settings-state settings-state-error settings-save-error"><AlertTriangle :size="17" /><span>{{ saveError }}</span><button v-if="conflict" class="text-button" type="button" @click="loadLogging">重新加载</button></div></template></div>
        </section>

        <section v-else class="settings-section" aria-labelledby="p115-title">
          <header class="settings-section-heading"><div><p class="eyebrow">P115 CONNECTOR</p><h2 id="p115-title">115 推送</h2></div><button class="icon-button" type="button" title="刷新 115 状态" aria-label="刷新 115 状态" :disabled="p115Loading" @click="loadP115"><RefreshCw :size="16" :class="{ spin: p115Loading }" /></button></header>
          <div v-if="p115Loading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载 115 状态</div>
          <div v-else-if="p115Error" class="settings-state settings-state-error"><AlertTriangle :size="18" /><span>{{ p115Error }}</span><button class="text-button" type="button" @click="loadP115">重试</button></div>
          <template v-else-if="p115">
            <div class="p115-status-line"><span class="settings-status-name"><Server :size="17" />连接状态</span><span :class="['p115-readiness', p115.readiness === 'ready' ? 'status-ok' : 'status-degraded']">{{ p115ReadinessLabel(p115.readiness) }}</span></div>
            <div class="settings-metrics p115-metrics"><div class="settings-metric"><span>启用</span><strong>{{ p115.enabled ? '是' : '否' }}</strong></div><div class="settings-metric"><span>Cookie 来源</span><strong>{{ cookieSourceLabel(p115.cookie_source) }}</strong></div><div class="settings-metric"><span>Cookie 结构</span><strong>{{ cookieStructureLabel(p115.cookie_structure) }}</strong></div><div class="settings-metric"><span>同步时间</span><strong>{{ p115.cookie_synced_at ? formatTimestamp(p115.cookie_synced_at) : '未知' }}</strong></div></div>
            <div class="settings-subsection"><h3>推送能力</h3><div class="settings-capability-list"><div><span>磁力云下载</span><strong :class="p115.capabilities.magnet ? 'status-ok' : 'status-degraded'">{{ p115.capabilities.magnet ? '可用' : '不可用' }}</strong></div><div><span>115 分享转存</span><strong class="status-degraded">未启用</strong></div></div></div>
            <div class="settings-action-row"><button class="secondary-button" type="button" :disabled="validationState === 'running'" @click="validateP115"><LoaderCircle v-if="validationState === 'running'" class="spin" :size="16" /><Cookie v-else :size="16" />验证 Cookie</button><span v-if="validationMessage" :class="['settings-action-message', validationState === 'error' ? 'status-down' : validationState === 'success' ? 'status-ok' : '']">{{ validationMessage }}</span></div>
            <p class="settings-note"><Cookie :size="15" />Cookie 仅使用服务端已配置的来源，页面不会读取或输入 Cookie 原文。</p>
          </template>
        </section>
      </div>
    </div>
  </section>
</template>
