<script setup lang="ts">
import { LoaderCircle, RefreshCw, StopCircle, Zap } from "@lucide/vue";
import { onMounted, ref } from "vue";
import { ApiClient, ApiError } from "../api";
import OrganizationResultPanel from "../components/OrganizationResultPanel.vue";
import type { OrganizationSettingsResponse } from "../types";
import OrganizationHistoryView from "./OrganizationHistoryView.vue";
import OrganizationWorkbenchView from "./OrganizationWorkbenchView.vue";

const props = withDefaults(defineProps<{ api: ApiClient; executionSupported?: boolean }>(), {
  executionSupported: false,
});

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
const orgSaving = ref(false);
const orgSaveError = ref("");
const orgDraft = ref({
  schedule_enabled: false,
  auto_execute_enabled: false,
  scan_interval_minutes: 30,
});
const actionBusy = ref(false);
const actionMessage = ref("");
const resultReloadToken = ref(0);
const reviewKey = ref(0);

async function loadSettings() {
  orgLoading.value = true;
  orgError.value = "";
  try {
    const response = await props.api.organizationSettings();
    orgSettings.value = response;
    orgDraft.value = {
      schedule_enabled: response.schedule_enabled,
      auto_execute_enabled: response.auto_execute_enabled,
      scan_interval_minutes: response.scan_interval_minutes,
    };
  } catch (exception) {
    orgError.value = exception instanceof ApiError ? exception.message : "整理设置加载失败，请稍后重试";
  } finally {
    orgLoading.value = false;
  }
}

async function saveSettings() {
  if (!orgSettings.value || orgSaving.value) return;
  orgSaving.value = true;
  orgSaveError.value = "";
  try {
    const response = await props.api.updateOrganizationSettings({
      ...orgDraft.value,
      revision: orgSettings.value.revision,
    });
    orgSettings.value = response;
    orgDraft.value = {
      schedule_enabled: response.schedule_enabled,
      auto_execute_enabled: response.auto_execute_enabled,
      scan_interval_minutes: response.scan_interval_minutes,
    };
  } catch (exception) {
    orgSaveError.value = exception instanceof ApiError ? exception.message : "整理设置保存失败，请稍后重试";
    await loadSettings();
  } finally {
    orgSaving.value = false;
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
    <p v-if="orgSaveError" class="settings-state settings-state-error" role="alert">{{ orgSaveError }}</p>

    <div class="organization-tabs" role="tablist" aria-label="整理视图">
      <button v-for="tab in tabs" :key="tab.value" type="button" :class="{ active: activeTab === tab.value }" @click="activeTab = tab.value">{{ tab.label }}</button>
    </div>

    <template v-if="activeTab === 'auto'">
      <div v-if="orgLoading" class="organization-empty"><LoaderCircle class="spin" :size="22" /><span>正在加载整理设置</span></div>
      <div v-else-if="orgError" class="organization-empty" role="alert">{{ orgError }}<button class="text-button" type="button" @click="loadSettings">重试</button></div>
      <template v-else>
        <div class="organization-auto-settings">
          <label class="settings-toggle"><input v-model="orgDraft.schedule_enabled" type="checkbox" @change="saveSettings" />定时整理：启用后按扫描间隔自动触发扫描</label>
          <label class="settings-toggle"><input v-model="orgDraft.auto_execute_enabled" type="checkbox" @change="saveSettings" />自动整理：识别高置信度的影片自动确认并排队，识别不确定的保留在来源目录，在「待处理」中人工处理</label>
          <div class="settings-form-grid"><label>扫描频率（分钟）<input v-model.number="orgDraft.scan_interval_minutes" type="number" min="5" max="1440" @change="saveSettings" /></label></div>
          <p class="settings-note">自动整理需要 115 写契约已验证；未满足时仅生成待处理计划，不会执行移动。更多目录与命名规则请到「设置 → 115整理」配置。</p>
        </div>
        <OrganizationResultPanel :api="props.api" :reload-token="resultReloadToken" />
      </template>
    </template>

    <OrganizationWorkbenchView v-else-if="activeTab === 'review'" :key="`review-${reviewKey}`" :api="props.api" :execution-supported="executionSupported" embedded />
    <OrganizationHistoryView v-else :key="'history'" :api="props.api" embedded />
  </section>
</template>
