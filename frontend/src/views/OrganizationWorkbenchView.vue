<script setup lang="ts">
import { Ban, Check, ChevronRight, Eye, ListChecks, LoaderCircle, Play, RefreshCw, Search, Tag } from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { ApiClient, ApiError, focusFirstFieldError } from "../api";
import { describeUiError } from "../errorCatalog";
import type { OrganizationExecutionBlocker, OrganizationOperationResponse, OrganizationPlanStatus, OrganizationPlanSummary } from "../types";

const props = withDefaults(defineProps<{ api: ApiClient; enabled?: boolean; executionEnabled?: boolean }>(), {
  enabled: true,
  executionEnabled: false,
});

const activeStatus = ref<OrganizationPlanStatus>("needs_review");
const items = ref<OrganizationPlanSummary[]>([]);
const nextCursor = ref<number | null>(null);
const selected = ref<OrganizationPlanSummary | null>(null);
const operation = ref<OrganizationOperationResponse | null>(null);
const aliasInput = ref("");
const searchQuery = ref("");
const searchSourceIndex = ref(0);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

const selectedIsReviewable = computed(() => selected.value?.status === "needs_review");
const selectedCanEdit = computed(() => selected.value?.status === "needs_review" || selected.value?.status === "planned");
const selectedCanExecute = computed(() => selected.value?.can_execute === true);
const executableItems = computed(() => items.value.filter((item) => item.can_execute));
const selectedExecutionBlockers = computed<OrganizationExecutionBlocker[]>(() => {
  const blockers = selected.value?.execution_blockers;
  if (blockers?.length) return blockers;
  return selected.value && !selected.value.can_execute
    ? [{
        kind: "status",
        code: "plan_not_executable",
        message_zh: "当前计划尚不能执行。",
        next_step_zh: "刷新计划并按提示完成复核或重新生成计划。",
      }]
    : [];
});

const statusLabel: Record<OrganizationPlanStatus, string> = {
  needs_review: "待确认",
  planned: "已确认（本地预览）",
  invalidated: "已失效",
  ignored: "已忽略",
};

const operationStatusLabel: Record<OrganizationOperationResponse["status"], string> = {
  planned: "已排队",
  organizing: "执行中",
  organized: "已完成",
  failed: "已失败",
  uncertain: "结果待确认",
  cancelled: "已取消",
};

function normalizePlan(plan: OrganizationPlanSummary): OrganizationPlanSummary {
  return {
    ...plan,
    candidates: Array.isArray(plan.candidates) ? plan.candidates : [],
    execution_blockers: Array.isArray(plan.execution_blockers) ? plan.execution_blockers : [],
  };
}

function operationFailureMessage(code: string | null): string {
  if (code === "plan_prerequisites_changed") return "扫描快照已更新，原计划已失效。请重新扫描并生成新的整理计划后再确认。";
  if (code === "postcondition_mismatch") return "远端结果未满足计划预期，系统已停止后续写入，请先核对 115 当前状态。";
  if (code === "uncertain" || code === "outcome_unknown") return "远端结果暂时无法确认，请先核对 115 当前状态，不要重复提交。";
  return "整理操作未完成，请查看当前状态后再决定下一步。";
}

async function loadPlanOperation(plan: OrganizationPlanSummary | null) {
  operation.value = null;
  const getter = props.api.organizationPlanOperation;
  if (!plan || typeof getter !== "function") return;
  try {
    operation.value = await getter.call(props.api, plan.plan_id);
  } catch (exception) {
    if (!(exception instanceof ApiError) || exception.code !== "operation_not_found") {
      operation.value = null;
    }
  }
}

async function loadPlans(cursor?: number) {
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.organizationPlans({ status: activeStatus.value, cursor, limit: 20 });
    items.value = response.items.map(normalizePlan);
    nextCursor.value = response.next_cursor;
    selected.value = items.value[0] ?? null;
    aliasInput.value = selected.value?.alias ?? "";
    await loadPlanOperation(selected.value);
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "计划列表加载失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}

function selectPlan(plan: OrganizationPlanSummary) {
  selected.value = plan;
  aliasInput.value = plan.alias ?? "";
  searchQuery.value = "";
  searchSourceIndex.value = 0;
  notice.value = "";
}

async function refreshAfterConflict() {
  await loadPlans();
  notice.value = "计划版本已变化，已刷新当前列表";
}

async function confirmPlan() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedIsReviewable.value || !selectedCanExecute.value) return;
  await mutate("confirm", () => props.api.confirmOrganizationPlan(plan.plan_id, plan.revision));
}

async function ignorePlan() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedCanEdit.value) return;
  await mutate("ignore", () => props.api.ignoreOrganizationPlan(plan.plan_id, plan.revision));
}

async function saveAlias() {
  const plan = selected.value;
  if (!plan || busy.value || !selectedCanEdit.value) return;
  await mutate("alias", () => props.api.aliasOrganizationPlan(plan.plan_id, aliasInput.value, plan.revision));
}

async function queueOperation() {
  const plan = selected.value;
  if (!plan || busy.value || plan.status !== "planned" || !selectedCanExecute.value || !props.executionEnabled) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const queuedOperation = await props.api.queueOrganizationOperation(plan.plan_id, plan.revision);
    await loadPlanOperation(plan);
    if (!operation.value) operation.value = queuedOperation;
    await handleQueuedOperation(queuedOperation);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (exception instanceof ApiError && exception.code === "plan_prerequisites_changed") {
      await loadPlanOperation(plan);
    }
    error.value = exception instanceof ApiError ? exception.message : "整理操作排队失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}

async function confirmAndQueueOperation() {
  const plan = selected.value;
  if (!plan || busy.value || plan.status !== "needs_review" || !selectedCanExecute.value || !props.executionEnabled) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const queuedOperation = await props.api.confirmAndQueueOrganizationOperation(plan.plan_id, plan.revision);
    selected.value = { ...plan, status: "planned", revision: plan.revision + 1 };
    await loadPlanOperation(selected.value);
    if (!operation.value) operation.value = queuedOperation;
    await handleQueuedOperation(queuedOperation);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (exception instanceof ApiError && exception.status === 409) {
      await refreshAfterConflict();
    } else {
      error.value = exception instanceof ApiError ? exception.message : "确认并整理失败，请稍后重试";
    }
  } finally {
    busy.value = false;
  }
}

async function selectCandidate(candidate: OrganizationPlanSummary["candidates"][number]) {
  const plan = selected.value;
  if (!plan || busy.value || plan.status !== "needs_review") return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await props.api.selectOrganizationCandidate(
      plan.plan_id,
      plan.revision,
      candidate.source_object_id,
      candidate.tmdb_id,
    );
    selected.value = normalizePlan(updated);
    items.value = items.value.map((item) => item.plan_id === plan.plan_id ? normalizePlan(updated) : item);
    notice.value = updated.status === "planned"
      ? "已生成可执行计划，请确认后开始整理"
      : "已选择影片，但分类或归档路径仍需检查";
  } catch (exception) {
    error.value = exception instanceof ApiError ? exception.message : "选择影片失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}

async function searchCandidates() {
  const plan = selected.value;
  const search = props.api.searchOrganizationCandidates;
  if (
    !plan
    || busy.value
    || plan.status !== "needs_review"
    || typeof search !== "function"
    || !searchQuery.value.trim()
  ) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await search.call(
      props.api,
      plan.plan_id,
      plan.revision,
      searchQuery.value.trim(),
      searchSourceIndex.value,
    );
    selected.value = normalizePlan(updated);
    items.value = items.value.map((item) => item.plan_id === plan.plan_id ? normalizePlan(updated) : item);
    notice.value = updated.candidates.length
      ? "已找到候选，请选择正确影片"
      : "未找到候选，请尝试更具体的片名或年份";
  } catch (exception) {
    focusFirstFieldError(exception);
    error.value = exception instanceof ApiError ? exception.message : "候选搜索失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}

async function handleQueuedOperation(queuedOperation: OrganizationOperationResponse) {
  if (queuedOperation.status === "organized") {
    notice.value = "整理已完成";
  } else if (queuedOperation.status === "failed" || queuedOperation.status === "uncertain") {
    error.value = queuedOperation.error_code
      ? describeUiError(queuedOperation.error_code, 409).message
      : "后台整理未完成，请查看操作状态";
  } else {
    notice.value = "整理已提交，后台正在执行";
    await pollOperation(queuedOperation.operation_id);
  }
}

async function confirmAndQueueCurrentPage() {
  const executable = executableItems.value;
  if (!props.executionEnabled || activeStatus.value !== "needs_review" || !executable.length || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  const skipped = items.value.length - executable.length;
  try {
    const response = await props.api.confirmAndQueueOrganizationOperations(
      executable.map((item) => ({ planId: item.plan_id, expectedRevision: item.revision })),
    );
    const accepted = response.items.filter((item) => item.status !== "rejected").length;
    const rejected = response.items.filter((item) => item.status === "rejected");
    await loadPlans();
    if (accepted) notice.value = `已确认并提交 ${accepted} 个整理计划，后台正在执行${skipped ? `，跳过 ${skipped} 个待搜索或复核计划` : ""}`;
    else if (skipped) notice.value = `当前页没有新的整理操作，已跳过 ${skipped} 个待搜索或复核计划`;
    if (rejected.length) {
      error.value = `${rejected.length} 个计划未提交：${rejected[0].message}`;
    }
  } catch (exception) {
    focusFirstFieldError(exception);
    error.value = exception instanceof ApiError ? exception.message : "批量确认并整理失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}

async function pollOperation(operationId: string) {
  if (typeof props.api.organizationOperation !== "function") return;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    try {
      const current = await props.api.organizationOperation(operationId);
      operation.value = current;
      if (current.status === "organized") {
        notice.value = "整理已完成";
        return;
      }
      if (current.status === "failed" || current.status === "uncertain") {
        error.value = current.error_code
          ? describeUiError(current.error_code, 409).message
          : "后台整理未完成，请查看操作状态";
        notice.value = "";
        return;
      }
    } catch {
      return;
    }
  }
}

async function mutate(action: "confirm" | "ignore" | "alias", operation: () => Promise<OrganizationPlanSummary>) {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await operation();
    selected.value = normalizePlan(updated);
    aliasInput.value = updated.alias ?? "";
    await loadPlanOperation(updated);
    items.value = items.value.map((item) => item.plan_id === updated.plan_id ? normalizePlan(updated) : item);
    notice.value = action === "confirm" ? "已确认本地计划，未执行远端写操作" : action === "ignore" ? "已忽略本地计划" : "本地别名已保存";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (exception instanceof ApiError && exception.status === 409) {
      await refreshAfterConflict();
    } else {
      error.value = exception instanceof ApiError ? exception.message : "操作失败，请稍后重试";
    }
  } finally {
    busy.value = false;
  }
}

async function changeStatus(status: OrganizationPlanStatus) {
  if (busy.value || activeStatus.value === status) return;
  activeStatus.value = status;
  await loadPlans();
}

onMounted(() => {
  if (props.enabled) void loadPlans();
});
</script>

<template>
  <section v-if="enabled" class="organization-workbench">
    <div class="organization-heading">
      <div>
        <p class="eyebrow">本地审核</p>
        <h1>整理计划工作台</h1>
        <p>{{ executionEnabled ? "可执行计划可一次确认并进入后台整理。" : "这里只改变本地计划状态，不会执行远端操作。" }}</p>
      </div>
      <button class="icon-button" type="button" title="刷新计划" aria-label="刷新计划" :disabled="loading || busy" @click="loadPlans()"><RefreshCw :size="17" :class="{ spin: loading }" /></button>
    </div>

    <p v-if="error" class="error-strip"><Ban :size="16" />{{ error }}</p>
    <p v-if="notice" class="success-strip"><Check :size="16" />{{ notice }}</p>

    <div class="organization-tabs" role="tablist" aria-label="计划状态">
      <button type="button" :class="{ active: activeStatus === 'needs_review' }" @click="changeStatus('needs_review')">待确认</button>
      <button type="button" :class="{ active: activeStatus === 'planned' }" @click="changeStatus('planned')">已确认</button>
      <button type="button" :class="{ active: activeStatus === 'ignored' }" @click="changeStatus('ignored')">已忽略</button>
      <button type="button" :class="{ active: activeStatus === 'invalidated' }" @click="changeStatus('invalidated')">已失效</button>
    </div>

    <div v-if="loading && !items.length" class="organization-empty"><LoaderCircle class="spin" :size="22" /><span>正在加载计划</span></div>
    <div v-else-if="!items.length" class="organization-empty"><Eye :size="22" /><strong>暂无计划</strong><span>当前状态没有可展示的本地计划。</span></div>
    <div v-else class="organization-layout">
      <div class="organization-list" aria-label="计划列表">
        <button v-if="executionEnabled && activeStatus === 'needs_review'" class="primary-button organization-batch-action" type="button" :disabled="loading || busy" @click="confirmAndQueueCurrentPage"><ListChecks :size="16" />确认并整理当前页（{{ items.length }}）</button>
        <button v-for="plan in items" :key="plan.plan_id" type="button" class="organization-plan-row" :class="{ active: selected?.plan_id === plan.plan_id }" @click="selectPlan(plan)">
          <span class="organization-plan-row-main"><strong>{{ plan.alias || `计划 ${plan.plan_id.slice(0, 8)}` }}</strong><small>{{ statusLabel[plan.status] }}</small></span>
          <span class="organization-plan-row-meta"><span>版本 {{ plan.revision }}</span><ChevronRight :size="16" /></span>
        </button>
        <button v-if="nextCursor !== null" class="secondary-button organization-more" type="button" :disabled="loading || busy" @click="loadPlans(nextCursor!)">加载下一页</button>
      </div>

      <article v-if="selected" class="organization-preview">
        <div class="organization-preview-heading"><div><p class="eyebrow">整理计划预览</p><h2>{{ selected.alias || "未命名计划" }}</h2></div><span class="organization-status">{{ statusLabel[selected.status] }}</span></div>
        <dl class="organization-facts">
          <div><dt>计划标识</dt><dd>{{ selected.plan_id }}</dd></div>
          <div><dt>版本</dt><dd>{{ selected.revision }}</dd></div>
          <div><dt>来源条目</dt><dd>{{ selected.source_count }}</dd></div>
          <div><dt>预览动作</dt><dd>{{ selected.action_count }}</dd></div>
          <div><dt>可执行移动</dt><dd>{{ selected.executable_action_count }}</dd></div>
          <div><dt>待复核动作</dt><dd>{{ selected.review_action_count }}</dd></div>
          <div><dt>前置条件</dt><dd>{{ selected.precondition_count }}</dd></div>
        </dl>
        <section v-if="!selected.can_execute" class="organization-execution-blockers" aria-live="polite">
          <strong>当前不能执行</strong>
          <ul><li v-for="blocker in selectedExecutionBlockers" :key="`${blocker.kind}-${blocker.code}`"><span>{{ blocker.message_zh }}</span><small>下一步：{{ blocker.next_step_zh }}</small></li></ul>
        </section>
        <div v-if="selected.status === 'needs_review' && selected.candidates.length" class="organization-candidate-list">
          <strong>请选择识别结果</strong>
          <button v-for="candidate in selected.candidates" :key="`${candidate.source_object_id}-${candidate.tmdb_id}`" type="button" class="organization-candidate" :disabled="busy" @click="selectCandidate(candidate)">
            <span>{{ candidate.title }}</span><small>{{ candidate.media_type === 'tv' ? '剧集' : '电影' }}<template v-if="candidate.release_year"> · {{ candidate.release_year }}</template></small>
          </button>
        </div>
        <form v-if="selected.status === 'needs_review'" class="organization-candidate-search" @submit.prevent="searchCandidates">
          <div class="organization-candidate-search-heading"><strong>没有合适结果？手动搜索 TMDB</strong><small>搜索结果只用于本地预览，选择后再确认整理</small></div>
          <label v-if="selected.source_count > 1" for="organization-source-index">来源条目
            <select id="organization-source-index" v-model.number="searchSourceIndex" :disabled="busy">
              <option v-for="index in selected.source_count" :key="index - 1" :value="index - 1">来源条目 {{ index }}</option>
            </select>
          </label>
          <div class="organization-candidate-search-controls"><input id="organization-candidate-query" v-model="searchQuery" type="search" maxlength="200" placeholder="输入片名或年份" autocomplete="off" /><button class="secondary-button" type="submit" :disabled="busy || !searchQuery.trim()"><LoaderCircle v-if="busy" class="spin" :size="15" /><Search v-else :size="15" />搜索候选</button></div>
        </form>
        <p class="organization-safe-note">预览只显示本地摘要。</p>
        <div v-if="operation" class="organization-operation-status" :class="{ failed: operation.status === 'failed', uncertain: operation.status === 'uncertain' }">
          <strong>整理操作：{{ operationStatusLabel[operation.status] }}</strong>
          <span v-if="operation.status === 'failed'">{{ operationFailureMessage(operation.error_code) }}</span>
          <span v-else-if="operation.status === 'uncertain'">{{ operationFailureMessage(operation.error_code) }}</span>
          <span v-else-if="operation.status === 'organizing'">后台正在执行，页面刷新后仍会保留当前状态。</span>
        </div>
        <div v-if="selectedCanEdit || (selected.status === 'planned' && executionEnabled)" class="organization-actions">
          <button v-if="selectedIsReviewable && executionEnabled && selectedCanExecute" class="primary-button" type="button" :disabled="busy" @click="confirmAndQueueOperation"><Play :size="16" />确认并开始整理</button>
          <button v-else-if="selectedIsReviewable && selectedCanExecute" class="primary-button" type="button" :disabled="busy" @click="confirmPlan"><Check :size="16" />确认本地计划</button>
          <button v-if="selected.status === 'planned' && executionEnabled && selectedCanExecute" class="primary-button" type="button" :disabled="busy" @click="queueOperation"><Play :size="16" />立即整理</button>
          <button class="secondary-button" type="button" :disabled="busy" @click="ignorePlan"><Ban :size="16" />忽略</button>
        </div>
        <form v-if="selectedCanEdit" class="organization-alias" @submit.prevent="saveAlias">
          <label for="organization-alias-input"><Tag :size="16" />本地别名</label>
          <div><input id="organization-alias-input" name="alias" v-model="aliasInput" maxlength="64" autocomplete="off" placeholder="仅用于本地标记" /><button class="secondary-button" type="submit" :disabled="busy || !aliasInput.trim()">保存</button></div>
        </form>
      </article>
    </div>
  </section>
</template>
