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

async function runNow() {
  if (actionBusy.value) return;
  actionBusy.value = true;
  actionMessage.value = "";
  try {
    const response = await props.api.runOrganizationNow();
    actionMessage.value = response.message_zh;
    resultReloadToken.value += 1;
  } catch (exception) {
    actionMessage.value = exception instanceof ApiError ? exception.message : "开始整理失败，请稍后重试";
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
        <p>自动整理：高置信度影片在定时运行中自动确认并排队；「开始整理」只生成待处理预览，识别不确定的文件留在源目录。</p>
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
          <p class="settings-note">自动整理规则在「设置 → 115整理」统一配置，这里只展示当前生效的调度状态。</p>
          <div class="organization-summary-list">
            <span>定时整理<strong :class="orgSettings?.schedule_enabled ? 'status-ok' : 'status-degraded'">{{ orgSettings?.schedule_enabled ? '已开启' : '已关闭' }}</strong></span>
            <span>自动整理<strong :class="orgSettings?.auto_execute_enabled ? 'status-ok' : 'status-degraded'">{{ orgSettings?.auto_execute_enabled ? '已开启' : '已关闭' }}</strong></span>
            <span>扫描频率<strong>{{ orgSettings?.scan_interval_minutes ?? '—' }} 分钟</strong></span>
          </div>
          <button class="secondary-button" type="button" @click="emit('open-settings')">前往设置配置整理规则</button>
          <p class="settings-note">自动整理需要 115 写契约已验证；未满足时仅生成待处理计划，不会执行移动。</p>
        </div>
        <OrganizationResultPanel :api="props.api" :reload-token="resultReloadToken" />
      </template>
    </template>

    <OrganizationWorkbenchView v-else-if="activeTab === 'review'" :key="`review-${reviewKey}`" :api="props.api" :execution-supported="executionSupported" embedded />
    <OrganizationHistoryView v-else :key="'history'" :api="props.api" embedded />
  </section>
</template>

<style scoped>
.organization-summary-list {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 24px;
  padding: 12px 0;
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
</style>
