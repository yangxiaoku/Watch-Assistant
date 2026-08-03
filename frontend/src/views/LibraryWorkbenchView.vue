<script setup lang="ts">
import { Ban, Check, Database, LoaderCircle, RefreshCw, SlidersHorizontal, Trash2 } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError, createIdempotencyKey, focusFirstFieldError } from "../api";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import PaginationBar from "../components/PaginationBar.vue";
import { describeUiError } from "../errorCatalog";
import { libraryScanStatusLabel, manifestStatusLabel, strmOperationNextStep, strmOperationStatusLabel } from "../statusCatalog";
import { diagnosticReference } from "../uiSafety";
import type {
  MediaEntryResponse,
  MediaLibraryResponse,
  EmptyDirectoryCleanupPlanResponse,
  CapabilityAvailability,
  LibraryScanSummary,
  StrmCleanupPlanResponse,
  StrmGenerationResponse,
  StrmManifestItemResponse,
  StrmOperationResponse,
} from "../types";

const props = defineProps<{
  api: ApiClient;
  organizationPlanCapability?: CapabilityAvailability;
  strmFullCapability?: CapabilityAvailability;
  strmIncrementalCapability?: CapabilityAvailability;
  strmCleanupCapability?: CapabilityAvailability;
  emptyDirectoryCleanupCapability?: CapabilityAvailability;
}>();
const emit = defineEmits<{
  "open-settings": [section: "overview" | "organization"];
}>();

const libraries = ref<MediaLibraryResponse[]>([]);
const selectedId = ref<string | null>(null);
const media = ref<MediaEntryResponse[]>([]);
const mediaNextCursor = ref<number | null>(null);
const manifest = ref<StrmManifestItemResponse[]>([]);
const operations = ref<StrmOperationResponse[]>([]);
const operationsNextCursor = ref<string | null>(null);
const mediaLoading = ref(false);
const mediaError = ref("");
const manifestPage = ref(1);
const manifestPageSize = ref(50);
const manifestTotal = ref(0);
const manifestTotalPages = ref(1);
const manifestLoading = ref(false);
const manifestError = ref("");
const operationsLoading = ref(false);
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
const pendingCleanup = ref<"strm" | "empty" | "operation" | null>(null);
const operationDetailOpen = ref(false);
let operationPollGeneration = 0;
let operationPollTimer: number | null = null;
  let scanPollGeneration = 0;
let libraryRequestGeneration = 0;
let mediaRequestGeneration = 0;
let manifestRequestGeneration = 0;
let operationsRequestGeneration = 0;
let operationDetailRequestGeneration = 0;

const selected = computed(() => libraries.value.find((item) => item.library_id === selectedId.value) ?? null);
const scan = computed(() => selected.value?.latest_scan ?? null);
const readyForSync = computed(() => Boolean(selected.value?.enabled && selected.value.scope_verified && scan.value?.complete && scan.value.state === "completed"));
const organizationPlanAvailable = computed(() => props.organizationPlanCapability?.enabled ?? false);
const strmFullAvailable = computed(() => props.strmFullCapability?.enabled ?? false);
const strmIncrementalAvailable = computed(() => props.strmIncrementalCapability?.enabled ?? false);
const strmCleanupAvailable = computed(() => props.strmCleanupCapability?.enabled ?? false);
const emptyDirectoryCleanupAvailable = computed(() => props.emptyDirectoryCleanupCapability?.enabled ?? false);
const cleanupDialogOpen = computed(() => pendingCleanup.value !== null);
const cleanupDialogTitle = computed(() => {
  if (pendingCleanup.value === "empty") return "确认回收空目录";
  if (pendingCleanup.value === "operation") return "确认取消 STRM 操作";
  return "确认退休失效 STRM";
});
const cleanupDialogSummary = computed(() => pendingCleanup.value === "empty"
  ? "系统只会把计划中的受管空目录送入可恢复回收站，永久删除保持关闭。"
  : pendingCleanup.value === "operation"
    ? "只会请求取消尚未完成的 STRM 操作；已经写入或结果待确认的内容不会被伪造撤回。"
    : "系统只会退休计划中受管且未被用户修改的失效 STRM 文件，扫描或清单变化会阻断执行。"
);
const cleanupDialogDetails = computed(() => {
  if (pendingCleanup.value === "operation" && latestOperation.value) {
    return [
      `当前状态：${strmOperationStatusLabel(latestOperation.value)}`,
      "服务端会再次核对操作状态，状态已变化时将拒绝取消。",
      "取消失败或结果不确定时，请刷新状态，不要重复提交。",
    ];
  }
  if (pendingCleanup.value === "empty" && emptyCleanupPlan.value) {
    const plan = emptyCleanupPlan.value;
    return [
      `候选目录：${plan.candidate_count} 项，可恢复回收：${plan.executable_count} 项`,
      `已阻断：${plan.blocked_count} 项；快照修订：${plan.source_snapshot_revision}`,
      "执行前仍会复核计划版本、摘要和幂等键。",
    ];
  }
  if (pendingCleanup.value === "strm" && cleanupPlan.value) {
    const plan = cleanupPlan.value;
    return [
      `候选 STRM：${plan.candidate_count} 项，可退休：${plan.executable_count} 项`,
      `已阻断：${plan.blocked_count} 项；快照修订：${plan.source_snapshot_revision}`,
      "执行前仍会复核计划版本、摘要和幂等键。",
    ];
  }
  return [];
});

const workflowGuidance = computed(() => {
  if (!selected.value) return "下一步：初始化媒体库，保存配置、验证范围并完成首次扫描。";
  if (!selected.value.enabled) return "媒体库尚未启用。下一步：保存配置并验证范围。";
  if (!selected.value.scope_verified) return "媒体库范围尚未验证。下一步：点击“验证范围”。";
  if (!scan.value || scan.value.state !== "completed" || !scan.value.complete) return "尚未完成完整扫描，STRM 和清理保持禁用。下一步：完成一次完整扫描。";
  return "";
});

function actionReason(action: "scan" | "organization" | "full" | "incremental" | "strm-cleanup" | "empty-cleanup"): string {
  if (!selected.value) return "请先初始化媒体库。";
  if (action === "scan" && (!selected.value.enabled || !selected.value.scope_verified)) {
    return selected.value.enabled ? "请先验证媒体库范围。" : "请先启用媒体库并保存配置。";
  }
  if (!readyForSync.value) return workflowGuidance.value || "请先完成媒体库准备流程。";
  if (action === "organization" && !organizationPlanAvailable.value) return props.organizationPlanCapability?.reason_zh || "自动整理计划当前不可用。";
  if (action === "full" && !strmFullAvailable.value) return props.strmFullCapability?.reason_zh || "STRM 全量生成当前不可用。";
  if (action === "incremental" && !strmIncrementalAvailable.value) return props.strmIncrementalCapability?.reason_zh || "STRM 增量同步当前不可用。";
  if (action === "strm-cleanup" && !strmCleanupAvailable.value) return props.strmCleanupCapability?.reason_zh || "STRM 失效清理当前不可用。";
  if (action === "empty-cleanup" && !emptyDirectoryCleanupAvailable.value) return props.emptyDirectoryCleanupCapability?.reason_zh || "空目录回收当前不可用。";
  return "";
}

function openCapabilitySettings(capability: CapabilityAvailability | undefined, fallback: "overview" | "organization") {
  emit("open-settings", capability?.settings_section ?? fallback);
}

function setError(exception: unknown, fallback: string) {
  error.value = exception instanceof ApiError ? exception.message : fallback;
}

function isTerminalScan(scanSummary: LibraryScanSummary): boolean {
  return ["completed", "failed", "cancelled"].includes(scanSummary.state);
}

function updateLibraryScan(libraryId: string, scanSummary: LibraryScanSummary): void {
  libraries.value = libraries.value.map((item) => item.library_id === libraryId
    ? { ...item, latest_scan: scanSummary }
    : item);
}

async function pollLibraryScan(
  libraryId: string,
  initial: LibraryScanSummary,
  generation: number,
): Promise<LibraryScanSummary | null> {
  let current = initial;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (generation !== scanPollGeneration || selectedId.value !== libraryId) return null;
    if (isTerminalScan(current)) return current;
    await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
    if (generation !== scanPollGeneration || selectedId.value !== libraryId) return null;
    current = await props.api.getLibraryScan(libraryId, current.run_id);
    updateLibraryScan(libraryId, current);
  }
  return current;
}

async function monitorLibraryScan(libraryId: string, initial: LibraryScanSummary): Promise<LibraryScanSummary | null> {
  if (isTerminalScan(initial)) return initial;
  const generation = ++scanPollGeneration;
  const final = await pollLibraryScan(libraryId, initial, generation);
  if (final && generation === scanPollGeneration && selectedId.value === libraryId && isTerminalScan(final)) {
    await loadOutputs(libraryId);
  }
  return final;
}

function scanNotice(scanSummary: LibraryScanSummary): string {
  if (scanSummary.state === "completed" && scanSummary.complete) {
    return `扫描完成，共发现 ${scanSummary.items_seen} 项`;
  }
  if (scanSummary.state === "completed") {
    return "扫描已结束但快照未完成，STRM 和清理仍保持阻断";
  }
  if (scanSummary.state === "failed") {
    return scanSummary.error_message_zh || "扫描失败，请查看状态后重试";
  }
  if (scanSummary.state === "cancelled") {
    return scanSummary.error_message_zh || "扫描已取消，已保存的分页结果可以继续使用";
  }
  return "扫描仍在进行，状态会持续更新；完成后才能执行 STRM 和清理";
}

async function loadLibraries(preferredId = selectedId.value) {
  const requestGeneration = ++libraryRequestGeneration;
  loading.value = true;
  error.value = "";
  try {
    const response = await props.api.libraries();
    if (requestGeneration !== libraryRequestGeneration) return;
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
    if (requestGeneration === libraryRequestGeneration) setError(exception, "媒体库加载失败，请稍后重试");
  } finally {
    if (requestGeneration === libraryRequestGeneration) loading.value = false;
  }
}

async function loadOutputs(libraryId: string) {
  if (selectedId.value !== libraryId) return;
  mediaNextCursor.value = null;
  operationsNextCursor.value = null;
  mediaRequestGeneration += 1;
  manifestRequestGeneration += 1;
  operationsRequestGeneration += 1;
  operationsError.value = "";
  await Promise.all([loadMedia(libraryId), loadManifestPage(libraryId, 1), loadOperations(libraryId)]);
}

async function loadMedia(libraryId: string, cursor?: number): Promise<void> {
  const requestGeneration = ++mediaRequestGeneration;
  mediaLoading.value = true;
  mediaError.value = "";
  try {
    const response = cursor === undefined
      ? await props.api.libraryMedia(libraryId)
      : await props.api.libraryMedia(libraryId, cursor);
    if (selectedId.value !== libraryId || requestGeneration !== mediaRequestGeneration) return;
    const existingIds = new Set(media.value.map((item) => item.media_id));
    media.value = cursor === undefined
      ? response.items
      : [...media.value, ...response.items.filter((item) => !existingIds.has(item.media_id))];
    mediaNextCursor.value = response.next_cursor ?? null;
  } catch (exception) {
    if (selectedId.value === libraryId && requestGeneration === mediaRequestGeneration) mediaError.value = exception instanceof ApiError ? exception.message : "索引文件加载失败，请重试";
  } finally {
    if (selectedId.value === libraryId && requestGeneration === mediaRequestGeneration) mediaLoading.value = false;
  }
}

async function loadManifestPage(libraryId = selectedId.value, targetPage = manifestPage.value): Promise<void> {
  if (!libraryId) return;
  const requestGeneration = ++manifestRequestGeneration;
  manifestLoading.value = true;
  manifestError.value = "";
  try {
    const response = await props.api.strmManifest(libraryId, Math.max(1, targetPage), manifestPageSize.value);
    if (selectedId.value !== libraryId || requestGeneration !== manifestRequestGeneration) return;
    manifest.value = response.items;
    manifestPage.value = Math.max(1, response.page || targetPage);
    manifestPageSize.value = Math.max(1, response.page_size || manifestPageSize.value);
    manifestTotal.value = Math.max(0, response.total || 0);
    manifestTotalPages.value = Math.max(1, response.total_pages || 1);
  } catch (exception) {
    if (selectedId.value === libraryId && requestGeneration === manifestRequestGeneration) manifestError.value = exception instanceof ApiError ? exception.message : "STRM 清单加载失败，请重试";
  } finally {
    if (selectedId.value === libraryId && requestGeneration === manifestRequestGeneration) manifestLoading.value = false;
  }
}

async function loadOperations(libraryId: string, cursor?: string): Promise<void> {
  const requestGeneration = ++operationsRequestGeneration;
  operationsLoading.value = true;
  operationsError.value = "";
  try {
    const operationsResponse = cursor === undefined
      ? await props.api.strmOperations(libraryId)
      : await props.api.strmOperations(libraryId, cursor);
    if (selectedId.value !== libraryId || requestGeneration !== operationsRequestGeneration) return;
    const existingIds = new Set(operations.value.map((item) => item.operation_id));
    operations.value = cursor === undefined
      ? operationsResponse.items
      : [...operations.value, ...operationsResponse.items.filter((item) => !existingIds.has(item.operation_id))];
    operationsNextCursor.value = operationsResponse.next_cursor ?? null;
    if (cursor === undefined && operations.value[0]) latestOperation.value = operations.value[0];
    else if (!latestOperation.value && operations.value[0]) latestOperation.value = operations.value[0];
  } catch {
    if (selectedId.value === libraryId && requestGeneration === operationsRequestGeneration) operationsError.value = "STRM 操作历史加载失败，请重试";
  } finally {
    if (selectedId.value === libraryId && requestGeneration === operationsRequestGeneration) operationsLoading.value = false;
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
  scanPollGeneration += 1;
  operationPollGeneration += 1;
  operationDetailRequestGeneration += 1;
  mediaRequestGeneration += 1;
  manifestRequestGeneration += 1;
  operationsRequestGeneration += 1;
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
  pendingCleanup.value = null;
  manifestPage.value = 1;
  manifest.value = [];
  manifestTotal.value = 0;
  manifestTotalPages.value = 1;
  media.value = [];
  mediaNextCursor.value = null;
  mediaLoading.value = false;
  manifestError.value = "";
  manifestLoading.value = false;
  mediaError.value = "";
  operationDetailOpen.value = false;
  operationsError.value = "";
  operationsLoading.value = false;
  operations.value = [];
  operationsNextCursor.value = null;
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
    const libraryId = selected.value.library_id;
    const result = await props.api.scanLibrary(libraryId);
    updateLibraryScan(libraryId, result);
    const final = await monitorLibraryScan(libraryId, result);
    if (!final || selectedId.value !== libraryId) return;
    notice.value = scanNotice(final);
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
      if (operation.failed > 0) return `STRM ${label}同步部分完成，生成 ${operation.generated} 个，未变化 ${operation.unchanged} 个，失败 ${operation.failed} 个，请查看失败统计并按需重试`;
      return `STRM ${label}同步完成，生成 ${operation.generated} 个，未变化 ${operation.unchanged} 个`;
    case "failed":
      return `STRM ${label}同步失败，请查看详情后重试`;
    case "timeout":
      return `STRM ${label}同步结果待确认，请先刷新并核对结果，确认前不要恢复执行`;
    case "cancelled":
      return `STRM ${label}同步已取消，可重试`;
  }
}

async function refreshLatestOperation() {
  const current = latestOperation.value;
  const libraryId = selectedId.value;
  if (!current || !libraryId) return;
  const requestGeneration = ++operationDetailRequestGeneration;
  try {
    const refreshed = await props.api.strmOperation(current.operation_id);
    if (selectedId.value !== libraryId || requestGeneration !== operationDetailRequestGeneration || latestOperation.value?.operation_id !== current.operation_id) return;
    latestOperation.value = refreshed;
    notice.value = operationStatusMessage(refreshed);
  } catch (exception) {
    if (selectedId.value === libraryId && requestGeneration === operationDetailRequestGeneration) setError(exception, "STRM 操作详情加载失败，请重试");
  }
}

function requestCancelLatestOperation() {
  const operation = latestOperation.value;
  if (!operation || (operation.status !== "queued" && operation.status !== "running")) return;
  pendingCleanup.value = "operation";
}

async function cancelLatestOperation() {
  const operation = latestOperation.value;
  if (!operation || (operation.status !== "queued" && operation.status !== "running")) return;
  operationPollGeneration += 1;
  if (operationPollTimer !== null) {
    window.clearTimeout(operationPollTimer);
    operationPollTimer = null;
  }
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const cancelled = await props.api.cancelStrmOperation(operation.operation_id);
    latestOperation.value = cancelled;
    latestResult.value = null;
    notice.value = operationStatusMessage(cancelled);
    if (selected.value) await loadOutputs(selected.value.library_id);
  } catch (exception) {
    setError(exception, "STRM 操作取消失败，请刷新状态");
  } finally {
    busy.value = false;
  }
}

async function retryLatestOperation() {
  const operation = latestOperation.value;
  if (
    !operation
    || operation.kind === "cleanup"
    || (operation.status !== "failed" && operation.status !== "cancelled")
    || busy.value
  ) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  operationDetailOpen.value = false;
  const pollGeneration = ++operationPollGeneration;
  const action = operation.kind === "incremental" ? "incremental" : "full";
  try {
    const resumed = await props.api.resumeStrmOperation(operation.operation_id);
    latestOperation.value = resumed;
    const terminal = resumed.status === "queued" || resumed.status === "running"
      ? await pollStrmOperation(resumed.operation_id, pollGeneration, action)
      : resumed;
    if (!terminal || pollGeneration !== operationPollGeneration) return;
    latestOperation.value = terminal;
    if (terminal.status === "succeeded") {
      latestResult.value = {
        operation_id: terminal.operation_id,
        library_id: terminal.library_id,
        scan_run_id: terminal.source_scan_run_id,
        generated: terminal.generated,
        unchanged: terminal.unchanged,
        skipped: terminal.skipped,
        failed: terminal.failed,
        retired: terminal.retired,
      };
      notice.value = operationStatusMessage(terminal, action);
    } else {
      latestResult.value = null;
      error.value = operationStatusMessage(terminal, action);
    }
    await loadOutputs(operation.library_id);
  } catch (exception) {
    setError(exception, "STRM 操作恢复失败，请刷新状态");
  } finally {
    busy.value = false;
  }
}

async function previewCleanup() {
  if (!selected.value || !scan.value || !readyForSync.value || !strmCleanupAvailable.value || busy.value) return;
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
  pendingCleanup.value = "strm";
}

async function applyCleanup(): Promise<void> {
  const plan = cleanupPlan.value;
  if (!plan || plan.status !== "needs_review" || plan.executable_count === 0 || busy.value) return;
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
  if (!selected.value || !scan.value || !readyForSync.value || !emptyDirectoryCleanupAvailable.value || busy.value) return;
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
  pendingCleanup.value = "empty";
}

async function applyEmptyDirectoryCleanup(): Promise<void> {
  const plan = emptyCleanupPlan.value;
  if (!plan || plan.status !== "needs_review" || plan.executable_count === 0 || busy.value) return;
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

function closeCleanupDialog(): void {
  if (!busy.value) pendingCleanup.value = null;
}

async function confirmPendingCleanup(): Promise<void> {
  const pending = pendingCleanup.value;
  pendingCleanup.value = null;
  if (pending === "strm") await applyCleanup();
  if (pending === "empty") await applyEmptyDirectoryCleanup();
  if (pending === "operation") await cancelLatestOperation();
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

function operationError(operation: StrmOperationResponse): string | null {
  if (!operation.error_code) return null;
  const descriptor = describeUiError(operation.error_code);
  return `${descriptor.title}：${descriptor.suggestion}`;
}

async function createOrganizationPreview() {
  if (!selected.value || !scan.value || !readyForSync.value || !organizationPlanAvailable.value || busy.value) return;
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
  scanPollGeneration += 1;
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
          <div><span>扫描状态</span><strong>{{ libraryScanStatusLabel(scan) }}</strong></div>
          <div><span>文件数</span><strong>{{ scan?.items_seen ?? 0 }}</strong></div>
          <div><span>STRM 数</span><strong>{{ manifestTotal }}</strong></div>
        </div>
        <div class="library-action-row">
          <button class="primary-button" type="button" :title="actionReason('scan')" :disabled="busy || !selected.enabled || !selected.scope_verified" @click="scanLibrary"><RefreshCw :size="16" />扫描目录</button>
          <button class="secondary-button" type="button" :title="actionReason('organization')" :disabled="busy || !readyForSync || !organizationPlanAvailable" @click="createOrganizationPreview"><SlidersHorizontal :size="16" />生成整理预览</button>
          <button class="secondary-button" type="button" :title="actionReason('full')" :disabled="busy || !readyForSync || !strmFullAvailable" @click="syncStrm('full')"><Database :size="16" />全量 STRM</button>
           <button class="secondary-button" type="button" :title="actionReason('incremental')" :disabled="busy || !readyForSync || !strmIncrementalAvailable" @click="syncStrm('incremental')"><RefreshCw :size="16" />增量同步</button>
           <button class="secondary-button" type="button" :title="actionReason('strm-cleanup')" :disabled="busy || !readyForSync || !strmCleanupAvailable" @click="previewCleanup"><Ban :size="16" />预览失效清理</button>
           <button class="secondary-button" type="button" :title="actionReason('empty-cleanup')" :disabled="busy || !readyForSync || !emptyDirectoryCleanupAvailable" @click="previewEmptyDirectoryCleanup"><Trash2 :size="16" />预览空目录清理</button>
        </div>
        <div class="library-capability-notices">
          <p v-if="workflowGuidance" class="library-workflow-note"><RefreshCw :size="15" />{{ workflowGuidance }}</p>
          <p v-if="!organizationPlanAvailable" class="library-capability-note"><SlidersHorizontal :size="15" />{{ organizationPlanCapability?.reason_zh || "自动整理计划当前不可用。" }}<button class="text-button" type="button" @click="openCapabilitySettings(organizationPlanCapability, 'organization')"><SlidersHorizontal :size="14" />前往整理计划设置</button></p>
          <p v-if="!strmFullAvailable" class="library-capability-note"><Database :size="15" />{{ strmFullCapability?.reason_zh || "STRM 全量生成当前不可用。" }}<button class="text-button" type="button" @click="openCapabilitySettings(strmFullCapability, 'overview')"><SlidersHorizontal :size="14" />前往设置</button></p>
          <p v-if="!strmIncrementalAvailable" class="library-capability-note"><RefreshCw :size="15" />{{ strmIncrementalCapability?.reason_zh || "STRM 增量同步当前不可用。" }}<button class="text-button" type="button" @click="openCapabilitySettings(strmIncrementalCapability, 'overview')"><SlidersHorizontal :size="14" />前往设置</button></p>
          <p v-if="!strmCleanupAvailable" class="library-capability-note"><Ban :size="15" />{{ strmCleanupCapability?.reason_zh || "STRM 失效清理当前不可用。" }}<button class="text-button" type="button" @click="openCapabilitySettings(strmCleanupCapability, 'overview')"><SlidersHorizontal :size="14" />前往设置</button></p>
          <p v-if="!emptyDirectoryCleanupAvailable" class="library-capability-note"><Trash2 :size="15" />{{ emptyDirectoryCleanupCapability?.reason_zh || "空目录回收当前不可用。" }}<button class="text-button" type="button" @click="openCapabilitySettings(emptyDirectoryCleanupCapability, 'organization')"><SlidersHorizontal :size="14" />前往自动整理设置</button></p>
        </div>
        <p v-if="busy" class="library-progress"><LoaderCircle class="spin" :size="16" />正在处理当前媒体库</p>
        <div v-if="latestResult" class="library-result"><strong>最近一次同步</strong><span>生成 {{ latestResult.generated }}</span><span>未变化 {{ latestResult.unchanged }}</span><span>跳过 {{ latestResult.skipped }}</span><span>失败 {{ latestResult.failed }}</span><span>退休 {{ latestResult.retired }}</span></div>
        <section v-if="latestOperation" class="library-operation-section" :class="`is-${latestOperation.status}`">
          <div class="library-section-heading"><div><p class="eyebrow">持久操作状态</p><h3>{{ operationLabels[latestOperation.kind] }}</h3></div><strong>{{ strmOperationStatusLabel(latestOperation) }}</strong></div>
          <div class="library-operation-stats"><span>生成 {{ latestOperation.generated }}</span><span>未变化 {{ latestOperation.unchanged }}</span><span>跳过 {{ latestOperation.skipped }}</span><span>失败 {{ latestOperation.failed }}</span><span>退休 {{ latestOperation.retired }}</span></div>
          <p class="library-operation-next-step">下一步：{{ strmOperationNextStep(latestOperation) }}</p>
          <p v-if="operationError(latestOperation)" class="library-operation-error">{{ operationError(latestOperation) }}</p>
          <div class="library-operation-actions"><button class="text-button" type="button" @click="operationDetailOpen = !operationDetailOpen">{{ operationDetailOpen ? "收起详情" : "查看详情" }}</button><button v-if="['queued', 'running'].includes(latestOperation.status)" class="text-button" type="button" @click="requestCancelLatestOperation"><Ban :size="14" />取消操作</button><button v-if="['failed', 'cancelled'].includes(latestOperation.status) && latestOperation.kind !== 'cleanup'" class="text-button" type="button" @click="retryLatestOperation"><RefreshCw :size="14" />恢复执行</button><button class="text-button" type="button" @click="refreshLatestOperation"><RefreshCw :size="14" />刷新状态</button></div>
          <div v-if="operationDetailOpen" class="library-operation-detail"><small>操作标识：{{ diagnosticReference(latestOperation.operation_id) }}</small><small>创建 {{ new Date(latestOperation.created_at).toLocaleString() }}</small><small v-if="latestOperation.finished_at">结束 {{ new Date(latestOperation.finished_at).toLocaleString() }}</small></div>
        </section>
         <section v-if="cleanupPlan" class="library-cleanup-plan" :class="`is-${cleanupPlan.status}`">
          <div class="library-section-heading"><div><p class="eyebrow">失效清理预览</p><h3>受管 STRM 退休计划</h3></div><strong>{{ cleanupPlan.status === "needs_review" ? "待确认" : cleanupPlan.status === "applied" ? "已完成" : "已失效" }}</strong></div>
          <div class="library-operation-stats"><span>候选 {{ cleanupPlan.candidate_count }}</span><span>可执行 {{ cleanupPlan.executable_count }}</span><span>已阻断 {{ cleanupPlan.blocked_count }}</span><span>快照修订 {{ cleanupPlan.source_snapshot_revision }}</span></div>
          <p class="library-cleanup-note">仅处理系统受管且内容未被用户修改的 STRM；扫描不完整或清单变化时会自动阻断。</p>
          <button v-if="cleanupPlan.status === 'needs_review' && cleanupPlan.executable_count" class="danger-button" type="button" :disabled="busy" @click="confirmCleanup"><Check :size="16" />查看摘要并确认清理</button>
           <p v-else-if="cleanupPlan.status === 'needs_review'" class="library-muted">当前没有可执行的清理项。</p>
         </section>
         <section v-if="emptyCleanupPlan" class="library-cleanup-plan" :class="`is-${emptyCleanupPlan.status}`">
           <div class="library-section-heading"><div><p class="eyebrow">空目录清理预览</p><h3>受管目录可恢复回收</h3></div><strong>{{ emptyCleanupStatusLabel(emptyCleanupPlan.status) }}</strong></div>
           <div class="library-operation-stats"><span>候选 {{ emptyCleanupPlan.candidate_count }}</span><span>可回收 {{ emptyCleanupPlan.executable_count }}</span><span>已阻断 {{ emptyCleanupPlan.blocked_count }}</span><span>快照修订 {{ emptyCleanupPlan.source_snapshot_revision }}</span></div>
           <p class="library-cleanup-note">仅处理同一媒体库最新完整扫描中已记录为系统创建、且仍在受管范围内的空目录，并遵守根目录、源目录、归档目录和推送目录保护规则；确认后只进入可恢复回收站，永久删除保持关闭。</p>
           <div v-if="emptyCleanupPlan.candidates.length" class="library-empty-directory-list">
             <div v-for="candidate in emptyCleanupPlan.candidates" :key="candidate.directory_id" class="library-empty-directory-row">
               <span><strong>{{ candidate.name }}</strong><small>{{ candidate.path || "相对路径不可用" }}</small></span><small>{{ candidate.state === "ready" ? "可回收" : "已阻断" }}</small>
             </div>
           </div>
           <button v-if="emptyCleanupPlan.status === 'needs_review' && emptyCleanupPlan.executable_count" class="danger-button" type="button" :disabled="busy" @click="confirmEmptyDirectoryCleanup"><Check :size="16" />查看摘要并确认回收</button>
           <p v-else-if="emptyCleanupPlan.status === 'needs_review'" class="library-muted">当前没有可执行的受管空目录。</p>
         </section>
        <section v-if="operationsError || operations.length || operationsNextCursor !== null || operationsLoading" class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">操作历史</p><h3>STRM 操作</h3></div><span v-if="operations.length">{{ operations.length }} 条</span></div><p v-if="operationsError" class="library-operation-history-error" role="alert">{{ operationsError }} <button class="text-button" type="button" @click="retryOutputs">重试</button></p><div v-if="operationsLoading && !operations.length" class="library-output-loading" role="status"><LoaderCircle class="spin" :size="18" />正在加载操作历史</div><div v-if="operations.length" class="library-operation-list" :class="{ 'library-list-loading': operationsLoading }"><div v-for="operation in operations" :key="operation.operation_id" class="library-operation-row"><span><strong>{{ operationLabels[operation.kind] }}</strong><small>{{ strmOperationStatusLabel(operation) }} · {{ new Date(operation.created_at).toLocaleString() }}</small></span><span>生成 {{ operation.generated }} · 失败 {{ operation.failed }}</span></div></div><p v-else-if="!operationsError && !operationsLoading" class="library-muted">暂无 STRM 操作记录。</p><button v-if="operationsNextCursor !== null" class="secondary-button" type="button" :disabled="operationsLoading" @click="selectedId && loadOperations(selectedId, operationsNextCursor!)">加载更多操作</button></section>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">受管清单</p><h3>STRM 文件</h3></div><span>{{ manifestTotal }} 条</span></div><p v-if="manifestError" class="library-operation-history-error" role="alert">{{ manifestError }} <button class="text-button" type="button" @click="loadManifestPage()">重试</button></p><div v-if="manifestLoading && !manifest.length" class="library-output-loading" role="status"><LoaderCircle class="spin" :size="18" />正在加载 STRM 清单</div><div v-if="manifest.length" class="library-table-wrap" :class="{ 'library-list-loading': manifestLoading }"><table><thead><tr><th>云端路径</th><th>本地路径</th><th>状态</th></tr></thead><tbody><tr v-for="item in manifest" :key="item.manifest_id"><td>{{ item.cloud_relative_path }}</td><td>{{ item.local_relative_path }}</td><td>{{ manifestStatusLabel(item.status) }}</td></tr></tbody></table></div><p v-else-if="!manifestError && !manifestLoading" class="library-muted">暂无受管 STRM。完成扫描后可以执行全量生成。</p><PaginationBar :page="manifestPage" :total-pages="manifestTotalPages" :total-results="manifestTotal" :loading="manifestLoading" @page="(page) => loadManifestPage(selectedId, page)" /></section>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">最近扫描</p><h3>索引文件</h3></div><span>{{ media.length }} 项</span></div><p v-if="mediaError" class="library-operation-history-error" role="alert">{{ mediaError }} <button class="text-button" type="button" @click="selectedId && loadMedia(selectedId, mediaNextCursor ?? undefined)">重试</button></p><div v-if="mediaLoading && !media.length" class="library-output-loading" role="status"><LoaderCircle class="spin" :size="18" />正在加载索引文件</div><div v-if="media.length" class="library-table-wrap" :class="{ 'library-list-loading': mediaLoading }"><table><thead><tr><th>文件名</th><th>大小</th><th>修改时间</th></tr></thead><tbody><tr v-for="item in media" :key="item.media_id"><td>{{ item.name }}</td><td>{{ formatBytes(item.size_bytes) }}</td><td>{{ item.modified_at ? new Date(item.modified_at).toLocaleString() : "未知" }}</td></tr></tbody></table></div><p v-else-if="!mediaError && !mediaLoading" class="library-muted">完成一次完整扫描后，这里会显示索引文件。</p><button v-if="mediaNextCursor !== null" class="secondary-button" type="button" :disabled="mediaLoading" @click="selectedId && loadMedia(selectedId, mediaNextCursor!)">加载更多索引文件</button></section>
      </main>
      <div v-else class="library-empty"><Database :size="24" /><strong>尚未配置库存媒体库</strong><span>下一步：读取服务器配置的 115 根目录，保存配置、验证范围，再完成首次扫描。</span><button class="primary-button" type="button" :disabled="busy" @click="initializeLibrary"><Database :size="16" />初始化并扫描媒体库</button><button class="text-button" type="button" @click="openCapabilitySettings(undefined, 'organization')"><SlidersHorizontal :size="14" />先检查 115 整理设置</button></div>
    </div>
    <ConfirmDialog :open="cleanupDialogOpen" :title="cleanupDialogTitle" :summary="cleanupDialogSummary" :details="cleanupDialogDetails" :confirm-label="pendingCleanup === 'operation' ? '确认取消' : '确认并提交'" tone="danger" :busy="busy" @cancel="closeCleanupDialog" @confirm="confirmPendingCleanup" />
  </section>
</template>
