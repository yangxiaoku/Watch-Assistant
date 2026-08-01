<script setup lang="ts">
import { Ban, Check, Database, LoaderCircle, RefreshCw, SlidersHorizontal, Trash2 } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError, createIdempotencyKey, focusFirstFieldError } from "../api";
import { describeUiError } from "../errorCatalog";
import type {
  MediaEntryResponse,
  MediaLibraryResponse,
  EmptyDirectoryCleanupPlanResponse,
  StrmCleanupPlanResponse,
  StrmGenerationResponse,
  StrmManifestItemResponse,
  StrmOperationResponse,
} from "../types";

const props = defineProps<{ api: ApiClient }>();

const libraries = ref<MediaLibraryResponse[]>([]);
const selectedId = ref<string | null>(null);
const media = ref<MediaEntryResponse[]>([]);
const manifest = ref<StrmManifestItemResponse[]>([]);
const operations = ref<StrmOperationResponse[]>([]);
const operationsError = ref("");
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const form = ref({ libraryId: "main", name: "115 媒体库", rootDirectoryId: "" });
const latestResult = ref<StrmGenerationResponse | null>(null);
const latestOperation = ref<StrmOperationResponse | null>(null);
const cleanupPlan = ref<StrmCleanupPlanResponse | null>(null);
const cleanupIdempotencyKey = ref<string | null>(null);
const emptyCleanupPlan = ref<EmptyDirectoryCleanupPlanResponse | null>(null);
const emptyCleanupIdempotencyKey = ref<string | null>(null);
const operationDetailOpen = ref(false);
let operationPollGeneration = 0;
let operationPollTimer: number | null = null;

const selected = computed(() => libraries.value.find((item) => item.library_id === selectedId.value) ?? null);
const scan = computed(() => selected.value?.latest_scan ?? null);
const readyForSync = computed(() => Boolean(selected.value?.enabled && scan.value?.complete && scan.value.state === "completed"));

function setError(exception: unknown, fallback: string) {
  error.value = exception instanceof ApiError ? exception.message : fallback;
}

async function loadLibraries(preferredId = selectedId.value) {
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.libraries();
    libraries.value = response.items;
    selectedId.value = response.items.some((item) => item.library_id === preferredId)
      ? preferredId
      : response.items[0]?.library_id ?? null;
    if (selected.value) {
      form.value = {
        libraryId: selected.value.library_id,
        name: selected.value.name,
        rootDirectoryId: selected.value.root_directory_id,
      };
      await loadOutputs(selected.value.library_id);
    }
  } catch (exception) {
    setError(exception, "媒体库加载失败，请稍后重试");
  } finally {
    loading.value = false;
  }
}

async function loadOutputs(libraryId: string) {
  operationsError.value = "";
  const [mediaResponse, manifestResponse] = await Promise.all([
    props.api.libraryMedia(libraryId).catch(() => ({ items: [], next_cursor: null })),
    props.api.strmManifest(libraryId).catch(() => ({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 })),
  ]);
  media.value = mediaResponse.items;
  manifest.value = manifestResponse.items;
  try {
    const operationsResponse = await props.api.strmOperations(libraryId);
    operations.value = operationsResponse.items;
  } catch {
    operationsError.value = "STRM 操作历史加载失败，请重试";
  }
}

async function retryOutputs() {
  if (selected.value) await loadOutputs(selected.value.library_id);
}

async function readConfiguredRoot() {
  const response = await props.api.p115Directories();
  const rootDirectoryId = response.parent_id.trim();
  if (!rootDirectoryId) throw new Error("missing_configured_root");
  form.value.rootDirectoryId = rootDirectoryId;
  return rootDirectoryId;
}

async function useConfiguredRoot() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await readConfiguredRoot();
    notice.value = "已读取服务器配置的 115 受控根目录，请保存媒体库配置";
  } catch (exception) {
    setError(exception, "受控根目录读取失败，请先检查 115 目标目录配置");
  } finally {
    busy.value = false;
  }
}

async function initializeLibrary() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await readConfiguredRoot();
    const current = libraries.value.find((item) => item.library_id === form.value.libraryId);
    const saved = await props.api.configureLibrary(form.value.libraryId, {
      name: form.value.name.trim(),
      root_directory_id: form.value.rootDirectoryId,
      revision: current?.revision ?? 0,
    });
    libraries.value = current
      ? libraries.value.map((item) => item.library_id === saved.library_id ? saved : item)
      : [...libraries.value, saved];
    selectedId.value = saved.library_id;

    const verification = await props.api.verifyLibraryScope(saved.library_id);
    libraries.value = libraries.value.map((item) => item.library_id === verification.library.library_id ? verification.library : item);
    const result = await props.api.scanLibrary(saved.library_id);
    libraries.value = libraries.value.map((item) => item.library_id === saved.library_id ? { ...item, latest_scan: result } : item);
    await loadOutputs(saved.library_id);
    notice.value = result.complete
      ? `媒体库已启用并完成首次扫描，共发现 ${result.items_seen} 项，现在可以重新推送`
      : "媒体库已验证，但首次扫描未完成，推送仍保持阻断";
  } catch (exception) {
    focusFirstFieldError(exception);
    setError(exception, "媒体库初始化失败，请检查 115 登录和目标目录配置");
  } finally {
    busy.value = false;
  }
}

function selectLibrary(library: MediaLibraryResponse) {
  operationPollGeneration += 1;
  if (operationPollTimer !== null) {
    window.clearTimeout(operationPollTimer);
    operationPollTimer = null;
  }
  selectedId.value = library.library_id;
  form.value = { libraryId: library.library_id, name: library.name, rootDirectoryId: library.root_directory_id };
  latestResult.value = null;
  latestOperation.value = null;
  cleanupPlan.value = null;
  cleanupIdempotencyKey.value = null;
  emptyCleanupPlan.value = null;
  emptyCleanupIdempotencyKey.value = null;
  operationDetailOpen.value = false;
  operationsError.value = "";
  operations.value = [];
  notice.value = "";
  void loadOutputs(library.library_id);
}

async function saveConfiguration() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const current = libraries.value.find((item) => item.library_id === form.value.libraryId);
    const saved = await props.api.configureLibrary(form.value.libraryId, {
      name: form.value.name.trim(),
      root_directory_id: form.value.rootDirectoryId.trim(),
      revision: current?.revision ?? 0,
    });
    libraries.value = current ? libraries.value.map((item) => item.library_id === saved.library_id ? saved : item) : [...libraries.value, saved];
    selectedId.value = saved.library_id;
    notice.value = "媒体库配置已保存，请先验证范围";
  } catch (exception) {
    focusFirstFieldError(exception);
    setError(exception, "媒体库配置保存失败");
  } finally {
    busy.value = false;
  }
}

async function verifyScope() {
  if (!selected.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const response = await props.api.verifyLibraryScope(selected.value.library_id);
    libraries.value = libraries.value.map((item) => item.library_id === response.library.library_id ? response.library : item);
    notice.value = response.enabled ? "媒体库范围已验证，可以开始扫描" : "媒体库范围尚未启用";
  } catch (exception) {
    setError(exception, "媒体库范围验证失败");
  } finally {
    busy.value = false;
  }
}

async function scanLibrary() {
  if (!selected.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await props.api.scanLibrary(selected.value.library_id);
    libraries.value = libraries.value.map((item) => item.library_id === selected.value?.library_id ? { ...item, latest_scan: result } : item);
    await loadOutputs(selected.value.library_id);
    notice.value = result.complete ? `扫描完成，共发现 ${result.items_seen} 项` : "扫描未完成，清理操作已保持阻断";
  } catch (exception) {
    setError(exception, "媒体库扫描失败");
  } finally {
    busy.value = false;
  }
}

async function syncStrm(action: "full" | "incremental") {
  if (!selected.value || !scan.value || !readyForSync.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  operationDetailOpen.value = false;
  const pollGeneration = ++operationPollGeneration;
  try {
    const result = action === "full"
      ? await props.api.generateStrm(selected.value.library_id, scan.value.run_id)
      : await props.api.incrementalStrm(selected.value.library_id, scan.value.run_id);
    const operation = await pollStrmOperation(result.operation_id, pollGeneration, action);
    if (!operation || pollGeneration !== operationPollGeneration) return;
    if (operation.status === "succeeded") {
      latestResult.value = result;
      notice.value = operationStatusMessage(operation, action);
    } else {
      latestResult.value = null;
      error.value = operationStatusMessage(operation, action);
    }
    await loadOutputs(selected.value.library_id);
  } catch (exception) {
    setError(exception, "STRM 操作失败");
  } finally {
    busy.value = false;
  }
}

function waitForOperationPoll() {
  return new Promise<void>((resolve) => {
    operationPollTimer = window.setTimeout(() => {
      operationPollTimer = null;
      resolve();
    }, 500);
  });
}

async function pollStrmOperation(
  operationId: string,
  pollGeneration: number,
  action: "full" | "incremental",
): Promise<StrmOperationResponse | null> {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (pollGeneration !== operationPollGeneration) return null;
    const operation = await props.api.strmOperation(operationId);
    if (pollGeneration !== operationPollGeneration) return null;
    latestOperation.value = operation;
    if (operation.status === "queued" || operation.status === "running") {
      notice.value = operationStatusMessage(operation, action);
      await waitForOperationPoll();
      continue;
    }
    return operation;
  }
  return latestOperation.value;
}

function operationStatusMessage(
  operation: StrmOperationResponse,
  action: "full" | "incremental" = operation.kind === "incremental" ? "incremental" : "full",
): string {
  const label = action === "full" ? "全量" : "增量";
  switch (operation.status) {
    case "queued":
      return `STRM ${label}同步已排队，正在等待执行`;
    case "running":
      return `STRM ${label}同步执行中，请稍候`;
    case "succeeded":
      return `STRM ${label}同步完成，生成 ${operation.generated} 个，未变化 ${operation.unchanged} 个`;
    case "failed":
      return `STRM ${label}同步失败，请查看详情后重试`;
    case "timeout":
      return `STRM ${label}同步超时，请查看详情后重试`;
    case "cancelled":
      return `STRM ${label}同步已取消，可重试`;
  }
}

async function refreshLatestOperation() {
  if (!latestOperation.value) return;
  try {
    latestOperation.value = await props.api.strmOperation(latestOperation.value.operation_id);
  } catch (exception) {
    setError(exception, "STRM 操作详情加载失败，请重试");
  }
}

async function retryLatestOperation() {
  const operation = latestOperation.value;
  if (!operation || (operation.status !== "failed" && operation.status !== "timeout" && operation.status !== "cancelled")) return;
  await syncStrm(operation.kind === "incremental" ? "incremental" : "full");
}

async function previewCleanup() {
  if (!selected.value || !scan.value || !readyForSync.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    cleanupPlan.value = await props.api.createStrmCleanupPlan(
      selected.value.library_id,
      scan.value.run_id,
    );
    cleanupIdempotencyKey.value = null;
    const plan = cleanupPlan.value;
    notice.value = plan.candidate_count
      ? `失效清理预览已生成，共 ${plan.candidate_count} 项，${plan.executable_count} 项可执行`
      : "失效清理预览已生成，没有需要退休的受管 STRM";
  } catch (exception) {
    setError(exception, "STRM 清理预览失败，请先完成一次完整扫描");
  } finally {
    busy.value = false;
  }
}

async function confirmCleanup() {
  const plan = cleanupPlan.value;
  if (!plan || plan.status !== "needs_review" || plan.executable_count === 0 || busy.value) return;
  if (typeof window !== "undefined" && !window.confirm("确认退休清理预览中的受管 STRM 文件？用户修改过的文件不会被删除。")) return;
  busy.value = true;
  error.value = "";
    notice.value = "";
  try {
    const idempotencyKey = cleanupIdempotencyKey.value ?? createIdempotencyKey();
    cleanupIdempotencyKey.value = idempotencyKey;
    const result = await props.api.applyStrmCleanupPlan(plan.plan_id, {
      expectedRevision: plan.revision,
      digest: plan.plan_hash,
      idempotencyKey,
    });
    cleanupPlan.value = result.plan;
    await loadOutputs(plan.library_id);
    notice.value = `失效清理已完成，退休 ${result.retired} 个受管 STRM`;
  } catch (exception) {
    setError(exception, "STRM 清理未执行，请重新扫描并生成预览");
  } finally {
    busy.value = false;
  }
}

async function previewEmptyDirectoryCleanup() {
  if (!selected.value || !scan.value || !readyForSync.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    emptyCleanupPlan.value = await props.api.createEmptyDirectoryCleanupPlan(
      selected.value.library_id,
      scan.value.run_id,
    );
    emptyCleanupIdempotencyKey.value = null;
    const plan = emptyCleanupPlan.value;
    notice.value = plan.candidate_count
      ? `空目录清理预览已生成，共 ${plan.candidate_count} 项，${plan.executable_count} 项可回收`
      : "空目录清理预览已生成，没有符合保护规则的受管空目录";
  } catch (exception) {
    setError(exception, "空目录清理预览失败，请先完成一次完整扫描");
  } finally {
    busy.value = false;
  }
}

async function confirmEmptyDirectoryCleanup() {
  const plan = emptyCleanupPlan.value;
  if (!plan || plan.status !== "needs_review" || plan.executable_count === 0 || busy.value) return;
  if (typeof window !== "undefined" && !window.confirm("确认将预览中的受管空目录可恢复回收到 115 回收站？永久删除保持关闭。")) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const idempotencyKey = emptyCleanupIdempotencyKey.value ?? createIdempotencyKey();
    emptyCleanupIdempotencyKey.value = idempotencyKey;
    const result = await props.api.applyEmptyDirectoryCleanupPlan(plan.plan_id, {
      expectedRevision: plan.revision,
      digest: plan.plan_hash,
      idempotencyKey,
    });
    emptyCleanupPlan.value = result.plan;
    notice.value = `空目录清理已完成，可恢复回收 ${result.deleted} 个受管目录`;
  } catch (exception) {
    setError(exception, "空目录清理未执行，请重新扫描并生成预览");
  } finally {
    busy.value = false;
  }
}

function emptyCleanupStatusLabel(status: EmptyDirectoryCleanupPlanResponse["status"]): string {
  switch (status) {
    case "needs_review": return "待确认";
    case "applying": return "执行中";
    case "applied": return "已完成";
    case "invalidated": return "已失效";
  }
}

const operationLabels: Record<StrmOperationResponse["kind"], string> = {
  full: "全量生成",
  incremental: "增量同步",
  cleanup: "失效清理",
};

const operationStatusLabels: Record<StrmOperationResponse["status"], string> = {
  queued: "已排队",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  timeout: "已超时",
  cancelled: "已取消",
};

function operationError(operation: StrmOperationResponse): string | null {
  if (!operation.error_code) return null;
  const descriptor = describeUiError(operation.error_code);
  return `${descriptor.title}：${descriptor.suggestion}`;
}

async function createOrganizationPreview() {
  if (!selected.value || !scan.value || !readyForSync.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const plan = await props.api.createOrganizationPreview(
      selected.value.library_id,
      scan.value.run_id,
    );
    notice.value = plan.status === "planned"
      ? "整理预览已生成，可在整理页面确认后执行"
      : "整理预览已生成，存在待确认项目，请到整理页面复核";
  } catch (exception) {
    setError(exception, "整理预览生成失败");
  } finally {
    busy.value = false;
  }
}

function formatBytes(value: number | null) {
  if (value === null) return "未知大小";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

onMounted(() => { void loadLibraries(); });
onBeforeUnmount(() => {
  operationPollGeneration += 1;
  if (operationPollTimer !== null) window.clearTimeout(operationPollTimer);
});
</script>

<template>
  <section class="library-workbench">
    <header class="library-workbench-heading">
      <div>
        <p class="eyebrow">115 媒体库</p>
        <h1>媒体库与 STRM</h1>
        <p>从受控目录扫描到 STRM 对账，所有写操作都基于已完成快照。</p>
      </div>
      <button class="icon-button" type="button" title="刷新媒体库" aria-label="刷新媒体库" :disabled="loading || busy" @click="loadLibraries()"><RefreshCw :size="17" :class="{ spin: loading }" /></button>
    </header>

    <p v-if="error" class="error-strip"><Ban :size="16" />{{ error }}</p>
    <p v-if="notice" class="success-strip"><Check :size="16" />{{ notice }}</p>

    <div class="library-workbench-layout">
      <aside class="library-scope-panel">
        <div class="library-panel-heading"><div><p class="eyebrow">受控范围</p><h2>媒体库</h2></div><Database :size="20" /></div>
        <div v-if="libraries.length" class="library-scope-list">
          <button v-for="item in libraries" :key="item.library_id" type="button" class="library-scope-row" :class="{ active: selectedId === item.library_id }" @click="selectLibrary(item)">
            <strong>{{ item.name }}</strong><small>{{ item.scope_verified ? "已验证" : "待验证" }} · {{ item.enabled ? "已启用" : "未启用" }}</small>
          </button>
        </div>
        <p v-else class="library-muted">还没有媒体库配置。</p>
        <form class="library-config-form" @submit.prevent="saveConfiguration">
          <label for="library-id">标识</label><input id="library-id" v-model="form.libraryId" name="libraryId" maxlength="128" :disabled="Boolean(selected)" required />
          <label for="library-name">名称</label><input id="library-name" v-model="form.name" name="name" maxlength="200" required />
          <label for="library-root">115 受控根目录</label><input id="library-root" v-model="form.rootDirectoryId" name="rootDirectoryId" inputmode="numeric" pattern="[1-9][0-9]*" placeholder="点击读取受控根目录" required /><button class="text-button" type="button" :disabled="busy" @click="useConfiguredRoot"><RefreshCw :size="14" />读取服务器配置的根目录</button>
          <button class="secondary-button" type="submit" :disabled="busy || !form.libraryId.trim() || !form.name.trim() || !form.rootDirectoryId.trim()"><SlidersHorizontal :size="16" />保存配置</button>
        </form>
      </aside>

      <main v-if="selected" class="library-detail-panel">
        <div class="library-detail-heading"><div><p class="eyebrow">当前范围</p><h2>{{ selected.name }}</h2><small>根目录 {{ selected.root_directory_id }}</small></div><button class="secondary-button" type="button" :disabled="busy || selected.scope_verified" @click="verifyScope"><Check :size="16" />验证范围</button></div>
        <div class="library-stat-grid">
          <div><span>范围状态</span><strong>{{ selected.scope_verified ? "已验证" : "待验证" }}</strong></div>
          <div><span>扫描状态</span><strong>{{ scan?.complete ? "完整" : scan?.state ?? "未扫描" }}</strong></div>
          <div><span>文件数</span><strong>{{ scan?.items_seen ?? 0 }}</strong></div>
          <div><span>STRM 数</span><strong>{{ manifest.length }}</strong></div>
        </div>
        <div class="library-action-row">
          <button class="primary-button" type="button" :disabled="busy || !selected.enabled || !selected.scope_verified" @click="scanLibrary"><RefreshCw :size="16" />扫描目录</button>
          <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="createOrganizationPreview"><SlidersHorizontal :size="16" />生成整理预览</button>
          <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="syncStrm('full')"><Database :size="16" />全量 STRM</button>
           <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="syncStrm('incremental')"><RefreshCw :size="16" />增量同步</button>
           <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="previewCleanup"><Ban :size="16" />预览失效清理</button>
           <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="previewEmptyDirectoryCleanup"><Trash2 :size="16" />预览空目录清理</button>
        </div>
        <p v-if="busy" class="library-progress"><LoaderCircle class="spin" :size="16" />正在处理当前媒体库</p>
        <div v-if="latestResult" class="library-result"><strong>最近一次同步</strong><span>生成 {{ latestResult.generated }}</span><span>未变化 {{ latestResult.unchanged }}</span><span>跳过 {{ latestResult.skipped }}</span><span>失败 {{ latestResult.failed }}</span><span>退休 {{ latestResult.retired }}</span></div>
        <section v-if="latestOperation" class="library-operation-section" :class="`is-${latestOperation.status}`">
          <div class="library-section-heading"><div><p class="eyebrow">持久操作状态</p><h3>{{ operationLabels[latestOperation.kind] }}</h3></div><strong>{{ operationStatusLabels[latestOperation.status] }}</strong></div>
          <div class="library-operation-stats"><span>生成 {{ latestOperation.generated }}</span><span>未变化 {{ latestOperation.unchanged }}</span><span>跳过 {{ latestOperation.skipped }}</span><span>失败 {{ latestOperation.failed }}</span><span>退休 {{ latestOperation.retired }}</span></div>
          <p v-if="operationError(latestOperation)" class="library-operation-error">{{ operationError(latestOperation) }}</p>
          <div class="library-operation-actions"><button class="text-button" type="button" @click="operationDetailOpen = !operationDetailOpen">{{ operationDetailOpen ? "收起详情" : "查看详情" }}</button><button v-if="['failed', 'timeout', 'cancelled'].includes(latestOperation.status)" class="text-button" type="button" @click="retryLatestOperation">重试</button><button class="text-button" type="button" @click="refreshLatestOperation">刷新状态</button></div>
          <div v-if="operationDetailOpen" class="library-operation-detail"><small>操作 {{ latestOperation.operation_id }}</small><small>创建 {{ new Date(latestOperation.created_at).toLocaleString() }}</small><small v-if="latestOperation.finished_at">结束 {{ new Date(latestOperation.finished_at).toLocaleString() }}</small></div>
        </section>
         <section v-if="cleanupPlan" class="library-cleanup-plan" :class="`is-${cleanupPlan.status}`">
          <div class="library-section-heading"><div><p class="eyebrow">失效清理预览</p><h3>受管 STRM 退休计划</h3></div><strong>{{ cleanupPlan.status === "needs_review" ? "待确认" : cleanupPlan.status === "applied" ? "已完成" : "已失效" }}</strong></div>
          <div class="library-operation-stats"><span>候选 {{ cleanupPlan.candidate_count }}</span><span>可执行 {{ cleanupPlan.executable_count }}</span><span>已阻断 {{ cleanupPlan.blocked_count }}</span><span>快照修订 {{ cleanupPlan.source_snapshot_revision }}</span></div>
          <p class="library-cleanup-note">仅处理系统受管且内容未被用户修改的 STRM；扫描不完整或清单变化时会自动阻断。</p>
          <button v-if="cleanupPlan.status === 'needs_review' && cleanupPlan.executable_count" class="danger-button" type="button" :disabled="busy" @click="confirmCleanup"><Check :size="16" />确认执行清理</button>
           <p v-else-if="cleanupPlan.status === 'needs_review'" class="library-muted">当前没有可执行的清理项。</p>
         </section>
         <section v-if="emptyCleanupPlan" class="library-cleanup-plan" :class="`is-${emptyCleanupPlan.status}`">
           <div class="library-section-heading"><div><p class="eyebrow">空目录清理预览</p><h3>受管目录可恢复回收</h3></div><strong>{{ emptyCleanupStatusLabel(emptyCleanupPlan.status) }}</strong></div>
           <div class="library-operation-stats"><span>候选 {{ emptyCleanupPlan.candidate_count }}</span><span>可回收 {{ emptyCleanupPlan.executable_count }}</span><span>已阻断 {{ emptyCleanupPlan.blocked_count }}</span><span>快照修订 {{ emptyCleanupPlan.source_snapshot_revision }}</span></div>
           <p class="library-cleanup-note">仅使用同一媒体库最新完整扫描中的系统受管目录，并遵守根目录、源目录、归档目录和推送目录保护规则；确认后只进入可恢复回收站，永久删除保持关闭。</p>
           <div v-if="emptyCleanupPlan.candidates.length" class="library-empty-directory-list">
             <div v-for="candidate in emptyCleanupPlan.candidates" :key="candidate.directory_id" class="library-empty-directory-row">
               <span><strong>{{ candidate.name }}</strong><small>{{ candidate.path || "相对路径不可用" }}</small></span><small>{{ candidate.state === "ready" ? "可回收" : "已阻断" }}</small>
             </div>
           </div>
           <button v-if="emptyCleanupPlan.status === 'needs_review' && emptyCleanupPlan.executable_count" class="danger-button" type="button" :disabled="busy" @click="confirmEmptyDirectoryCleanup"><Check :size="16" />确认可恢复回收</button>
           <p v-else-if="emptyCleanupPlan.status === 'needs_review'" class="library-muted">当前没有可执行的受管空目录。</p>
         </section>
        <section v-if="operationsError || operations.length" class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">操作历史</p><h3>STRM 操作</h3></div><span v-if="operations.length">{{ operations.length }} 条</span></div><p v-if="operationsError" class="library-operation-history-error" role="alert">{{ operationsError }} <button class="text-button" type="button" @click="retryOutputs">重试</button></p><div v-else class="library-operation-list"><div v-for="operation in operations" :key="operation.operation_id" class="library-operation-row"><span><strong>{{ operationLabels[operation.kind] }}</strong><small>{{ operationStatusLabels[operation.status] }} · {{ new Date(operation.created_at).toLocaleString() }}</small></span><span>生成 {{ operation.generated }} · 失败 {{ operation.failed }}</span></div></div></section>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">受管清单</p><h3>STRM 文件</h3></div><span>{{ manifest.length }} / 50</span></div><div v-if="manifest.length" class="library-table-wrap"><table><thead><tr><th>云端路径</th><th>本地路径</th><th>状态</th></tr></thead><tbody><tr v-for="item in manifest" :key="item.manifest_id"><td>{{ item.cloud_relative_path }}</td><td>{{ item.local_relative_path }}</td><td>{{ item.status }}</td></tr></tbody></table></div><p v-else class="library-muted">暂无受管 STRM。完成扫描后可以执行全量生成。</p></section>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">最近扫描</p><h3>索引文件</h3></div><span>{{ media.length }} / 50</span></div><div v-if="media.length" class="library-table-wrap"><table><thead><tr><th>文件名</th><th>大小</th><th>修改时间</th></tr></thead><tbody><tr v-for="item in media" :key="item.media_id"><td>{{ item.name }}</td><td>{{ formatBytes(item.size_bytes) }}</td><td>{{ item.modified_at ? new Date(item.modified_at).toLocaleString() : "未知" }}</td></tr></tbody></table></div><p v-else class="library-muted">完成一次完整扫描后，这里会显示索引文件。</p></section>
      </main>
      <div v-else class="library-empty"><Database :size="24" /><strong>尚未配置库存媒体库</strong><span>系统会读取服务器已配置的 115 根目录，保存、验证并完成首次扫描。</span><button class="primary-button" type="button" :disabled="busy" @click="initializeLibrary"><Database :size="16" />初始化并扫描媒体库</button></div>
    </div>
  </section>
</template>
