<script setup lang="ts">
import { AlertTriangle, Bell, Check, CheckCheck, CircleAlert, LoaderCircle, RefreshCw, ShieldAlert } from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import { formatTimestamp } from "../format";
import type { NotificationResponse, NotificationSeverity } from "../types";

const props = defineProps<{ api: ApiClient }>();
const emit = defineEmits<{ navigate: [view: "settings" | "workflows"] }>();

const filter = ref<"all" | "unread" | "actionable" | "error">("all");
const items = ref<NotificationResponse[]>([]);
const unreadCount = ref(0);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const preferencesEnabled = ref(true);
const preferenceRevision = ref(1);
const preferenceBusy = ref(false);
const quietHoursEnabled = ref(true);
const quietHoursStart = ref("23:00");
const quietHoursEnd = ref("08:00");
const quietHoursTimezone = ref("Asia/Shanghai");
const errorBypassQuietHours = ref(true);

const visibleItems = computed(() => items.value.filter((item) => {
  if (filter.value === "unread") return !item.read_at;
  if (filter.value === "error") return item.severity === "error" || item.severity === "security";
  if (filter.value === "actionable") return Boolean(item.action_type);
  return true;
}));

const filterOptions = [
  { value: "all", label: "全部" },
  { value: "unread", label: "未读" },
  { value: "actionable", label: "需要处理" },
  { value: "error", label: "错误与安全" },
] as const;

const severityLabels: Record<NotificationSeverity, string> = { info: "提示", warning: "警告", error: "错误", security: "安全" };
function severityLabel(value: NotificationSeverity): string { return severityLabels[value]; }
function severityIcon(value: NotificationSeverity) { return value === "security" ? ShieldAlert : value === "error" ? CircleAlert : Bell; }
function actionLabel(item: NotificationResponse): string {
  if (item.action_type === "workflow" || item.action_type === "task") return "查看任务";
  if (item.action_type === "settings") return "查看设置";
  return "查看详情";
}

async function loadNotifications() {
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.notifications(false, 100);
    items.value = response.items;
    unreadCount.value = response.unread_count;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "通知中心暂时无法加载";
  } finally {
    loading.value = false;
  }
}

async function loadPreferences() {
  try {
    const response = await props.api.notificationPreferences();
    preferencesEnabled.value = response.enabled;
    preferenceRevision.value = response.revision;
    quietHoursEnabled.value = response.quiet_hours_enabled ?? true;
    quietHoursStart.value = response.quiet_hours_start ?? "23:00";
    quietHoursEnd.value = response.quiet_hours_end ?? "08:00";
    quietHoursTimezone.value = response.quiet_hours_timezone ?? "Asia/Shanghai";
    errorBypassQuietHours.value = response.error_bypass_quiet_hours ?? true;
  } catch {
    // Notification preferences are optional; the list remains usable.
  }
}

async function saveQuietHours() {
  if (preferenceBusy.value) return;
  preferenceBusy.value = true;
  try {
    const response = await props.api.updateNotificationPreferences({
      quiet_hours_enabled: quietHoursEnabled.value,
      quiet_hours_start: quietHoursStart.value,
      quiet_hours_end: quietHoursEnd.value,
      quiet_hours_timezone: quietHoursTimezone.value,
      error_bypass_quiet_hours: errorBypassQuietHours.value,
      revision: preferenceRevision.value,
    });
    preferenceRevision.value = response.revision;
    quietHoursEnabled.value = response.quiet_hours_enabled ?? quietHoursEnabled.value;
    quietHoursStart.value = response.quiet_hours_start ?? quietHoursStart.value;
    quietHoursEnd.value = response.quiet_hours_end ?? quietHoursEnd.value;
    quietHoursTimezone.value = response.quiet_hours_timezone ?? quietHoursTimezone.value;
    errorBypassQuietHours.value = response.error_bypass_quiet_hours ?? errorBypassQuietHours.value;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "静默时段暂时无法保存";
  } finally {
    preferenceBusy.value = false;
  }
}

async function markRead(item: NotificationResponse) {
  if (item.read_at || busy.value) return;
  busy.value = true;
  try {
    const response = await props.api.markNotificationRead(item.id);
    const index = items.value.findIndex((candidate) => candidate.id === item.id);
    if (index >= 0) items.value[index] = response;
    unreadCount.value = Math.max(0, unreadCount.value - 1);
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "通知状态暂时无法更新";
  } finally {
    busy.value = false;
  }
}

async function markAllRead() {
  if (!unreadCount.value || busy.value) return;
  busy.value = true;
  try {
    await props.api.markAllNotificationsRead();
    items.value = items.value.map((item) => item.read_at ? item : { ...item, read_at: new Date().toISOString() });
    unreadCount.value = 0;
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "通知状态暂时无法更新";
  } finally {
    busy.value = false;
  }
}

async function togglePreferences() {
  if (preferenceBusy.value) return;
  const previous = preferencesEnabled.value;
  preferencesEnabled.value = !previous;
  preferenceBusy.value = true;
  try {
    const response = await props.api.updateNotificationPreferences({ enabled: preferencesEnabled.value, revision: preferenceRevision.value });
    preferencesEnabled.value = response.enabled;
    preferenceRevision.value = response.revision;
  } catch (exception) {
    preferencesEnabled.value = previous;
    error.value = exception instanceof ApiError ? exception.message : "通知偏好暂时无法保存";
  } finally {
    preferenceBusy.value = false;
  }
}

function openAction(item: NotificationResponse) {
  void markRead(item);
  if (item.action_type === "workflow" || item.action_type === "task") emit("navigate", "workflows");
  else if (item.action_type === "settings") emit("navigate", "settings");
}

onMounted(() => { void loadNotifications(); void loadPreferences(); });
</script>

<template>
  <section class="notification-center" aria-labelledby="notification-title">
    <header class="library-heading notification-heading"><div><p class="eyebrow">站内消息</p><h1 id="notification-title">通知中心 <span v-if="unreadCount" class="notification-count">{{ unreadCount }}</span></h1><p>查看任务、授权和系统状态的重要变化。</p></div><div class="notification-toolbar"><button class="icon-button" type="button" title="刷新通知" aria-label="刷新通知" :disabled="loading" @click="loadNotifications"><RefreshCw :size="17" :class="{ spin: loading }" /></button><button class="secondary-button" type="button" :disabled="!unreadCount || busy" @click="markAllRead"><CheckCheck :size="16" />全部已读</button></div></header>
    <div class="notification-controls"><div class="segmented" role="tablist" aria-label="通知筛选"><button v-for="option in filterOptions" :key="option.value" type="button" :class="{ active: filter === option.value }" @click="filter = option.value">{{ option.label }}</button></div><label class="notification-preference"><input type="checkbox" :checked="preferencesEnabled" :disabled="preferenceBusy" @change="togglePreferences" />接收站内通知</label><label class="notification-preference"><input v-model="quietHoursEnabled" type="checkbox" :disabled="preferenceBusy" @change="saveQuietHours" />静默时段</label><label class="notification-time"><span>开始</span><input v-model="quietHoursStart" type="time" :disabled="!quietHoursEnabled || preferenceBusy" @change="saveQuietHours" /></label><label class="notification-time"><span>结束</span><input v-model="quietHoursEnd" type="time" :disabled="!quietHoursEnabled || preferenceBusy" @change="saveQuietHours" /></label><label class="notification-preference"><input v-model="errorBypassQuietHours" type="checkbox" :disabled="!quietHoursEnabled || preferenceBusy" @change="saveQuietHours" />错误突破静默</label></div>
    <div v-if="error" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ error }}</span><button class="text-button" type="button" @click="loadNotifications">重试</button></div>
    <div v-if="loading && !items.length && !error" class="notification-empty" role="status"><LoaderCircle class="spin" :size="22" />正在加载通知</div>
    <div v-else-if="!visibleItems.length && !error" class="notification-empty"><Bell :size="22" />当前筛选没有通知</div>
    <div v-else-if="visibleItems.length" class="notification-list"><article v-for="item in visibleItems" :key="item.id" class="notification-item" :class="[`notification-${item.severity}`, { unread: !item.read_at }]" @click="markRead(item)"><component :is="severityIcon(item.severity)" :size="18" /><div class="notification-copy"><div class="notification-item-heading"><strong>{{ item.title_zh }}</strong><span>{{ severityLabel(item.severity) }}</span></div><p>{{ item.message_zh }}</p><small>{{ formatTimestamp(item.updated_at) }}<template v-if="item.aggregate_count > 1"> · 已聚合 {{ item.aggregate_count }} 次</template></small><div v-if="item.action_type" class="notification-action"><button class="text-button" type="button" @click.stop="openAction(item)">{{ actionLabel(item) }}</button></div></div><Check v-if="item.read_at" :size="16" class="notification-read" aria-label="已读" /></article></div>
  </section>
</template>
