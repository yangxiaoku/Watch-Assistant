<script setup lang="ts">
import { LoaderCircle, StopCircle, Zap } from "@lucide/vue";
import { onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import OrganizationResultPanel from "../components/OrganizationResultPanel.vue";
import type { OrganizationSettingsResponse } from "../types";
import OrganizationHistoryView from "./OrganizationHistoryView.vue";
import OrganizationWorkbenchView from "./OrganizationWorkbenchView.vue";

const props = withDefaults(defineProps<{ api: ApiClient; executionSupported?: boolean }>(), {
  executionSupported: false,
});
const emit = defineEmits<{ "open-settings": [] }>();

type Tab = "auto" | "review" | "history";
const activeTab = ref<Tab>("auto");
const tabs: Array<{ value: Tab; label: string }> = [
  { value: "auto", label: "状态" },
  { value: "review", label: "待处理" },
  { value: "history", label: "历史" },
];

const orgSettings = ref<OrganizationSettingsResponse | null>(null);
const orgLoading = ref(false);
const orgError = ref("");
const autoExecuting = ref(false);
const autoExecuteError = ref("");
const actionBusy = ref(false);
const actionMessage = ref("");
const resultReloadToken = ref(0);
const reviewKey = ref(0);

async function loadSettings() {
  orgLoading.value = true;
  orgError.value = "";
  try {
    orgSettings.value = await props.api.organizationSettings();
  } catch (exception) {
    orgError.value = exception instanceof ApiError ? exception.message : "整理设置加载失败，请稍后重试";
  } finally {
    orgLoading.value = false;
  }
}

async function toggleAutoExecute() {
  if (!orgSettings.value || autoExecuting.value) return;
  autoExecuting.value = true;
  autoExecuteError.value = "";
  try {
    const next = !orgSettings.value.auto_execute_enabled;
    orgSettings.value = await props.api.updateOrganizationSettings({
      auto_execute_enabled: next,
      revision: orgSettings.value.revision,
    });
  } catch (exception) {
    autoExecuteError.value = exception instanceof ApiError ? exception.message : "自动整理设置保存失败，请稍后重试";
    await loadSettings();
  } finally {
    autoExecuting.value = false;
  }
}

async function runNow() {
  if (actionBusy.value) return;
  actionBusy.value = true;
  actionMessage.value = "";
  try {
    const response = await props.api.runOrganizationNow();
    actionMessage.value = response.message_zh;
    resultReloadToken.value += 1;
  } catch (exception) {
    actionMessage.value = exception instanceof ApiError
      ? `${exception.message}${exception.suggestion ? ` ${exception.suggestion}` : ""}`
      : "开始整理失败，请稍后重试";
  } finally {
    actionBusy.value = false;
  }
}

async function stopSchedule() {
  if (actionBusy.value) return;
  actionBusy.value = true;
  actionMessage.value = "";
  try {
    const response = await props.api.stopOrganization();
    actionMessage.value = response.message_zh;
    await loadSettings();
  } catch (exception) {
    actionMessage.value = exception instanceof ApiError ? exception.message : "停止整理失败，请稍后重试";
  } finally {
    actionBusy.value = false;
  }
}

onMounted(() => void loadSettings());
</script>

<template>
  <section class="organization-view">
    <div class="organization-heading">
      <div>
        <p class="eyebrow">115 网盘</p>
        <h1>整理</h1>
        <p>自动整理：扫描已配置来源，高置信度影片自动确认并归档，识别不确定的保留在待处理中。</p>
      </div>
      <div class="organization-view-actions">
        <button class="primary-button" type="button" :disabled="actionBusy" @click="runNow"><LoaderCircle v-if="actionBusy" class="spin" :size="15" /><Zap v-else :size="15" />开始整理</button>
        <button class="secondary-button" type="button" :disabled="actionBusy" @click="stopSchedule"><StopCircle :size="15" />停止定时</button>
      </div>
    </div>

    <p v-if="actionMessage" class="settings-action-message" role="status">{{ actionMessage }}</p>

    <div class="organization-tabs" role="tablist" aria-label="整理视图">
      <button v-for="tab in tabs" :key="tab.value" type="button" :class="{ active: activeTab === tab.value }" @click="activeTab = tab.value">{{ tab.label }}</button>
    </div>

    <template v-if="activeTab === 'auto'">
      <div v-if="orgLoading" class="organization-empty"><LoaderCircle class="spin" :size="22" /><span>正在加载整理设置</span></div>
      <div v-else-if="orgError" class="organization-empty" role="alert">{{ orgError }}<button class="text-button" type="button" @click="loadSettings">重试</button></div>
      <template v-else>
        <div class="organization-auto-settings">
          <div class="organization-auto-card">
            <div><strong>自动整理</strong><span>{{ orgSettings?.auto_execute_enabled ? '已开启 —— 高置信度影片扫描后自动确认并归档' : '已关闭 —— 只生成待处理计划，不自动执行' }}</span></div>
            <button class="secondary-button" type="button" :disabled="autoExecuting" @click="toggleAutoExecute"><LoaderCircle v-if="autoExecuting" class="spin" :size="15" />{{ orgSettings?.auto_execute_enabled ? '关闭自动整理' : '开启自动整理' }}</button>
          </div>
          <p v-if="autoExecuteError" class="settings-state settings-state-error" role="alert">{{ autoExecuteError }}</p>
          <div class="organization-summary-list">
            <span>定时整理<strong :class="orgSettings?.schedule_enabled ? 'status-ok' : 'status-degraded'">{{ orgSettings?.schedule_enabled ? '已开启' : '已关闭' }}</strong></span>
            <span>扫描频率<strong>{{ orgSettings?.scan_interval_minutes ?? '—' }} 分钟</strong></span>
          </div>
          <p class="settings-note">自动整理需要 115 写契约已验证；未满足时仅生成待处理计划，不会执行移动。目录与命名规则在「设置 → 115整理」配置。</p>
          <button class="secondary-button" type="button" @click="emit('open-settings')">前往设置配置整理规则</button>
        </div>
        <OrganizationResultPanel :api="props.api" :reload-token="resultReloadToken" />
      </template>
    </template>

    <section v-else-if="activeTab === 'review'" class="organization-tab-panel">
      <header class="organization-tab-heading">
        <h2>待处理计划</h2>
        <p>下方按计划状态筛选：待确认为识别不确定的影片，已确认 / 已忽略 / 已失效查看对应状态。</p>
      </header>
      <OrganizationWorkbenchView :key="`review-${reviewKey}`" :api="props.api" :execution-supported="executionSupported" embedded />
    </section>

    <section v-else class="organization-tab-panel">
      <header class="organization-tab-heading">
        <h2>整理历史</h2>
        <p>已归档影片的记录，数据来自已完成的整理操作。</p>
      </header>
      <OrganizationHistoryView :key="'history'" :api="props.api" embedded />
    </section>
  </section>
</template>

<style scoped>
.organization-auto-settings {
  display: grid;
  gap: 12px;
  margin-bottom: 8px;
}
.organization-auto-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 11px;
}
.organization-auto-card strong {
  display: block;
  font-size: 15px;
  color: var(--text);
}
.organization-auto-card span {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-2);
}
.organization-summary-list {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 24px;
  padding: 8px 0;
}
.organization-summary-list span {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
}
.organization-summary-list strong {
  font-size: 14px;
  color: var(--text);
}
.organization-tab-panel {
  padding-top: 4px;
}
.organization-tab-heading {
  margin-bottom: 18px;
}
.organization-tab-heading h2 {
  margin-bottom: 6px;
  font-size: 22px;
  letter-spacing: -0.01em;
}
.organization-tab-heading p {
  margin: 0;
  color: var(--text-2);
  font-size: 13px;
}
</style>
