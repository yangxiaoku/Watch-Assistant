<script setup lang="ts">
import { Ban, Check, Database, LoaderCircle, RefreshCw, SlidersHorizontal } from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { ApiClient, ApiError, focusFirstFieldError } from "../api";
import type { MediaEntryResponse, MediaLibraryResponse, StrmGenerationResponse, StrmManifestItemResponse } from "../types";

const props = defineProps<{ api: ApiClient }>();

const libraries = ref<MediaLibraryResponse[]>([]);
const selectedId = ref<string | null>(null);
const media = ref<MediaEntryResponse[]>([]);
const manifest = ref<StrmManifestItemResponse[]>([]);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const form = ref({ libraryId: "main", name: "115 媒体库", rootDirectoryId: "" });
const latestResult = ref<StrmGenerationResponse | null>(null);

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
  const [mediaResponse, manifestResponse] = await Promise.all([
    props.api.libraryMedia(libraryId).catch(() => ({ items: [], next_cursor: null })),
    props.api.strmManifest(libraryId).catch(() => ({ items: [], page: 1, page_size: 50, total: 0, total_pages: 0 })),
  ]);
  media.value = mediaResponse.items;
  manifest.value = manifestResponse.items;
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
  selectedId.value = library.library_id;
  form.value = { libraryId: library.library_id, name: library.name, rootDirectoryId: library.root_directory_id };
  latestResult.value = null;
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

async function syncStrm(action: "full" | "incremental" | "cleanup") {
  if (!selected.value || !scan.value || !readyForSync.value || busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    latestResult.value = action === "full"
      ? await props.api.generateStrm(selected.value.library_id, scan.value.run_id)
      : action === "incremental"
        ? await props.api.incrementalStrm(selected.value.library_id, scan.value.run_id)
        : await props.api.cleanupStrm(selected.value.library_id, scan.value.run_id);
    await loadOutputs(selected.value.library_id);
    const result = latestResult.value;
    notice.value = action === "cleanup"
      ? `清理完成，退休 ${result.retired} 个受管条目`
      : `STRM 同步完成，生成 ${result.generated} 个，未变化 ${result.unchanged} 个`;
  } catch (exception) {
    setError(exception, "STRM 操作失败");
  } finally {
    busy.value = false;
  }
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
          <button class="secondary-button" type="button" :disabled="busy || !readyForSync" @click="syncStrm('cleanup')"><Ban :size="16" />清理失效</button>
        </div>
        <p v-if="busy" class="library-progress"><LoaderCircle class="spin" :size="16" />正在处理当前媒体库</p>
        <div v-if="latestResult" class="library-result"><strong>最近一次同步</strong><span>生成 {{ latestResult.generated }}</span><span>未变化 {{ latestResult.unchanged }}</span><span>跳过 {{ latestResult.skipped }}</span><span>失败 {{ latestResult.failed }}</span><span>退休 {{ latestResult.retired }}</span></div>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">受管清单</p><h3>STRM 文件</h3></div><span>{{ manifest.length }} / 50</span></div><div v-if="manifest.length" class="library-table-wrap"><table><thead><tr><th>云端路径</th><th>本地路径</th><th>状态</th></tr></thead><tbody><tr v-for="item in manifest" :key="item.manifest_id"><td>{{ item.cloud_relative_path }}</td><td>{{ item.local_relative_path }}</td><td>{{ item.status }}</td></tr></tbody></table></div><p v-else class="library-muted">暂无受管 STRM。完成扫描后可以执行全量生成。</p></section>
        <section class="library-output-section"><div class="library-section-heading"><div><p class="eyebrow">最近扫描</p><h3>索引文件</h3></div><span>{{ media.length }} / 50</span></div><div v-if="media.length" class="library-table-wrap"><table><thead><tr><th>文件名</th><th>大小</th><th>修改时间</th></tr></thead><tbody><tr v-for="item in media" :key="item.media_id"><td>{{ item.name }}</td><td>{{ formatBytes(item.size_bytes) }}</td><td>{{ item.modified_at ? new Date(item.modified_at).toLocaleString() : "未知" }}</td></tr></tbody></table></div><p v-else class="library-muted">完成一次完整扫描后，这里会显示索引文件。</p></section>
      </main>
      <div v-else class="library-empty"><Database :size="24" /><strong>尚未配置库存媒体库</strong><span>系统会读取服务器已配置的 115 根目录，保存、验证并完成首次扫描。</span><button class="primary-button" type="button" :disabled="busy" @click="initializeLibrary"><Database :size="16" />初始化并扫描媒体库</button></div>
    </div>
  </section>
</template>
