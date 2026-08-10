<script setup lang="ts">
import { AlertTriangle, LoaderCircle } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ApiClient, ApiError } from "../api";
import { pollUntil } from "../polling";
import { formatTimestamp } from "../format";
import { organizationResultItemStatusLabel, organizationResultStatusLabel } from "../statusCatalog";
import { diagnosticCode } from "../uiSafety";
import type { OrganizationAutomationResultResponse } from "../types";

const props = withDefaults(
  defineProps<{ api: ApiClient; reloadToken?: number; targetRunId?: string | null }>(),
  { reloadToken: 0, targetRunId: null },
);

const organizationResult = ref<OrganizationAutomationResultResponse | null>(null);
const organizationResultLoading = ref(false);
const organizationResultError = ref("");
const organizationResultPolling = ref(false);
const organizationResultPollTimeout = ref(false);
const organizationResultHeadline = computed(() => {
  // 目标运行尚未出现终态时,上一次的结果是旧数据,优先展示"进行中"而不是误导性的旧结论
  if (organizationResultPolling.value) return "整理进行中";
  const result = organizationResult.value;
  if (!result || result.status === "unknown") return "尚未整理";
  const statuses = (result.items ?? []).map((item) => item.status);
  if (statuses.some((status) => status === "queued" || status === "organizing")) return "整理进行中";
  if (statuses.some((status) => status === "needs_review" || status === "uncertain")) return "待确认，尚未移动文件";
  if (result.blocked_count > 0 || statuses.some((status) => status === "failed")) return "存在失败或阻断";
  if (result.queued_count > 0 && statuses.every((status) => status === "success" || status === "skipped")) return "整理完成";
  if (result.plan_count > 0 && result.queued_count === 0) return "扫描完成，未执行移动";
  return organizationResultStatusLabel(result.status);
});
const organizationResultStateClass = computed(() => {
  const headline = organizationResultHeadline.value;
  if (headline.includes("待确认")) return "is-needs-review";
  if (headline.includes("失败") || headline.includes("阻断")) return "is-failed";
  if (headline.includes("进行中")) return "is-running";
  if (headline.includes("尚未")) return "is-unknown";
  return "is-success";
});
const organizationResultSummary = computed(() => {
  const result = organizationResult.value;
  if (organizationResultPolling.value) return "本次整理仍在执行中，完成时间取决于来源数量和网络状况。";
  if (!result || result.status === "unknown") return "点击“开始整理”后，这里会显示扫描、识别和入库结果。";
  const items = result.items ?? [];
  const reviewCount = items.filter((item) => item.status === "needs_review" || item.status === "uncertain").length;
  const successCount = items.filter((item) => item.status === "success").length;
  if (reviewCount > 0) return `扫描已完成，${reviewCount} 个影片未完成识别，需要确认后才能移动。`;
  if (successCount > 0) return `已完成 ${successCount} 个影片的整理并写入归档目录。`;
  if (result.blocked_count > 0) return `扫描完成，但有 ${result.blocked_count} 个来源被阻断，未执行移动。`;
  if (result.plan_count > 0 && result.queued_count === 0) return "已生成整理计划，但没有影片进入移动队列。";
  return "本次扫描已完成。";
});
const organizationResultStatusBreakdown = computed(() => {
  const counts = new Map<string, number>();
  for (const item of organizationResult.value?.items ?? []) {
    counts.set(item.status, (counts.get(item.status) ?? 0) + 1);
  }
  return [...counts.entries()].map(([status, count]) => ({
    status,
    count,
    label: organizationResultItemStatusLabel(status as OrganizationAutomationResultResponse["items"][number]["status"]),
  }));
});

function redactDirectoryId(value: string | null | undefined): string {
  if (!value) return "未指定";
  if (value.length <= 6) return "***";
  return `${value.slice(0, 3)}***${value.slice(-3)}`;
}

let mounted = false;

async function loadOrganizationResult() {
  organizationResultLoading.value = true;
  organizationResultError.value = "";
  organizationResultPollTimeout.value = false;
  try {
    const response = await props.api.organizationResult();
    // 请求在途时组件已卸载(如切走 tab):丢弃响应,避免对已卸载面板启动轮询
    if (!mounted) return;
    organizationResult.value = response;
    startPollingIfNeeded();
  } catch (exception) {
    if (mounted) organizationResultError.value = exception instanceof ApiError ? exception.message : "整理结果加载失败，请稍后重试";
  } finally {
    if (mounted) organizationResultLoading.value = false;
  }
}

let pollActive = false;
let pollTask: Promise<void> | undefined;
function stopPolling(): void {
  pollActive = false;
  pollTask = undefined;
  organizationResultPolling.value = false;
}

/**
 * 后端 last_result 只在运行结束(finished_at=now)时写入,运行中返回的是上一次结果或 unknown,
 * 因此不能靠响应里的 run_id 判断"正在运行",必须以 run-now 返回的 run_id 为轮询目标:
 * 直到结果中出现同一 run_id 且 finished_at 非空,才算本次运行进入终态。
 */
function startPollingIfNeeded(): void {
  const targetRunId = props.targetRunId;
  const result = organizationResult.value;
  if (!targetRunId) return;
  if (result && result.run_id === targetRunId && result.finished_at !== null) return;
  startPolling();
}

function startPolling(): void {
  if (pollTask !== undefined || !mounted) return;
  const targetRunId = props.targetRunId;
  if (!targetRunId) return;
  pollActive = true;
  organizationResultPolling.value = true;
  organizationResultPollTimeout.value = false;
  pollTask = pollUntil(
    async () => {
      try {
        return await props.api.organizationResult();
      } catch (exception) {
        if (mounted && pollActive) {
          organizationResultError.value = exception instanceof ApiError ? exception.message : "整理结果暂时无法更新，请稍后重试";
        }
        throw exception;
      }
    },
    {
      intervalMs: 2000,
      maxAttempts: 90,
      isCurrent: () => pollActive && mounted,
      onResponse: (response) => {
        organizationResult.value = response;
      },
      isDone: (response) => response.run_id === targetRunId && response.finished_at !== null,
    },
  ).then((terminal) => {
    // pollUntil 返回 null 表示超时(或请求失败,失败已在上面 catch 提示过);
    // 只有"仍在轮询但超时"时才提示手动刷新,避免与报错提示互相覆盖。
    const wasActive = pollActive;
    pollTask = undefined;
    pollActive = false;
    organizationResultPolling.value = false;
    if (terminal === null && wasActive && mounted && !organizationResultError.value) {
      organizationResultPollTimeout.value = true;
    }
  });
}

watch(() => props.reloadToken, () => {
  stopPolling();
  void loadOrganizationResult();
});

watch(() => props.targetRunId, () => {
  // 新的 run-now 返回了新的 run_id:停止旧轮询,重新加载并针对新 run_id 轮询
  stopPolling();
  void loadOrganizationResult();
});

onMounted(() => {
  mounted = true;
  void loadOrganizationResult();
});

onBeforeUnmount(() => {
  mounted = false;
  stopPolling();
});
</script>

<template>
  <div class="organization-result-panel" aria-live="polite">
    <div class="organization-result-heading">
      <div><strong>最近一次整理结果</strong><p class="organization-result-summary">{{ organizationResultSummary }}</p></div>
      <div class="organization-result-meta"><span v-if="organizationResultLoading">正在更新</span><span v-else-if="organizationResultPolling">整理执行中</span><span v-else>{{ organizationResult?.finished_at ? formatTimestamp(organizationResult.finished_at) : '尚未执行' }}</span></div>
    </div>
    <div v-if="organizationResultError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ organizationResultError }}</span><button class="text-button" type="button" @click="loadOrganizationResult">重试</button></div>
    <p v-if="organizationResultPollTimeout" class="organization-result-timeout" role="status">等待整理完成超时，后台可能仍在执行，请手动刷新查看。<button class="text-button" type="button" @click="loadOrganizationResult">刷新</button></p>
    <div :class="['organization-result-state', organizationResultStateClass]">{{ organizationResultHeadline }}</div>
    <div v-if="organizationResultStatusBreakdown.length" class="organization-result-statuses" aria-label="影片处理状态">
      <span v-for="entry in organizationResultStatusBreakdown" :key="entry.status" :class="['organization-result-status', `is-${entry.status}`]">{{ entry.label }} <strong>{{ entry.count }}</strong></span>
    </div>
    <div v-if="organizationResult && organizationResult.status !== 'unknown'" class="organization-result-metrics">
      <span>来源目录 <strong>{{ organizationResult.source_count }}</strong></span><span>扫描成功 <strong>{{ organizationResult.scanned_count }}</strong></span><span>识别计划 <strong>{{ organizationResult.plan_count }}</strong></span><span>已开始整理 <strong>{{ organizationResult.queued_count }}</strong></span><span>未执行/阻断 <strong>{{ organizationResult.blocked_count }}</strong></span>
    </div>
    <div v-if="organizationResult?.items?.length" class="organization-result-items">
      <strong>影片处理结果</strong>
      <div v-for="item in organizationResult.items" :key="`${item.tmdb_id ?? item.title}-${item.target ?? ''}`" class="organization-result-item">
        <span><b>{{ item.title }}</b><small v-if="item.tmdb_id">TMDB {{ item.tmdb_id }}</small></span>
        <span :class="['organization-result-item-status', `is-${item.status}`]">{{ organizationResultItemStatusLabel(item.status) }}</span>
        <small v-if="item.target" class="organization-result-item-target">归档：{{ item.target }}</small><small v-else-if="item.status === 'needs_review' || item.status === 'uncertain'" class="organization-result-item-target">未生成归档路径，未移动文件</small>
        <details v-if="item.error_code"><summary>诊断信息</summary><small>错误码：{{ diagnosticCode(item.error_code) }}</small></details>
      </div>
    </div>
    <div v-if="organizationResult?.blocked_details?.length" class="organization-blocked-details"><strong>未执行原因</strong><ul><li v-for="detail in organizationResult.blocked_details" :key="`${detail.source_directory_id ?? 'automation'}-${detail.error_code}`"><span>{{ detail.source_directory_id ? '来源目录' : '自动整理' }}：{{ detail.message_zh }} 下一步：{{ detail.next_step_zh }}</span><details><summary>诊断信息</summary><small v-if="detail.source_directory_id">CID（脱敏）：{{ redactDirectoryId(detail.source_directory_id) }}；</small><small>阶段：{{ diagnosticCode(detail.phase) }}；错误码：{{ diagnosticCode(detail.error_code) }}</small></details></li></ul></div>
    <div v-if="organizationResultLoading" class="organization-result-loading"><LoaderCircle class="spin" :size="14" />正在更新</div>
  </div>
</template>
