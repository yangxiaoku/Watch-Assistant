<script setup lang="ts">
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cookie,
  ShieldAlert,
  FileText,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  Radio,
  Settings2,
  ScanSearch,
  ShieldCheck,
  Zap,
  XCircle,
} from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ApiClient, ApiError, focusFirstFieldError, isConflict, CONFLICT_MESSAGE_ZH } from "../api";
import { p115DeviceOptions } from "../p115DeviceTypes";
import { logLevelOptions } from "../statusCatalog";
import { diagnosticCode, diagnosticReference, safeLocalizedCopy } from "../uiSafety";
import type {
  CapabilityState,
  CapabilityAvailability,
  CapabilityStatusResponse,
  LogLevel,
  ContentPolicyResponse,
  CredentialSettingsResponse,
  LoggingSettingsResponse,
  InspectionSettingsResponse,
  OrganizationSettingsResponse,
  P115SettingsResponse,
  P115ValidationResponse,
  P115DirectoryItem,
  P115LoginDevice,
  SettingsOverviewResponse,
  PatchProwlarrSettingsRequest,
  ProwlarrSettingsResponse,
  ProwlarrVerifyResponse,
} from "../types";

const props = withDefaults(defineProps<{ api: ApiClient; initialSection?: SettingsSection }>(), {
  initialSection: "overview" as SettingsSection,
});
const emit = defineEmits<{ "auto-start-enabled": [enabled: boolean]; navigate: [view: "logs" | "organization"] }>();

type SettingsSection = "overview" | "credentials" | "prowlarr" | "logs" | "content" | "inspection" | "organization";
type ValidationState = "idle" | "running" | "error" | P115ValidationResponse["status"];

const sectionGroups = [
  {
    label: "系统",
    items: [
      { id: "overview" as const, label: "概览", icon: Activity },
      { id: "logs" as const, label: "日志", icon: FileText },
      { id: "content" as const, label: "内容安全", icon: ShieldAlert },
      { id: "inspection" as const, label: "资源检测", icon: ScanSearch },
    ],
  },
  {
    label: "连接与搜索",
    items: [
      { id: "credentials" as const, label: "连接配置", icon: KeyRound },
      { id: "prowlarr" as const, label: "搜索来源", icon: Radio },
    ],
  },
  {
    label: "入库与输出",
    items: [
      { id: "organization" as const, label: "115 整理", icon: Zap },
    ],
  },
];
const sections = sectionGroups.flatMap((group) => group.items);
const activeSection = ref<SettingsSection>(props.initialSection);
const overview = ref<SettingsOverviewResponse | null>(null);
const overviewLoading = ref(true);
const overviewError = ref("");
const p115 = ref<P115SettingsResponse | null>(null);
const p115Loading = ref(true);
const p115Error = ref("");
const credentials = ref<CredentialSettingsResponse | null>(null);
const credentialsLoading = ref(false);
const credentialsError = ref("");
const prowlarr = ref<ProwlarrSettingsResponse | null>(null);
const prowlarrLoading = ref(true);
const prowlarrError = ref("");
const prowlarrBaseUrlDraft = ref("");
const prowlarrApiKeyDraft = ref("");
const prowlarrEnabledDraft = ref(false);
const prowlarrSaving = ref(false);
const prowlarrResetting = ref(false);
const prowlarrSaveError = ref("");
const prowlarrSaveMessage = ref("");
const prowlarrConflict = ref(false);
const prowlarrValidationState = ref<"idle" | "running" | ProwlarrVerifyResponse["status"] | "error">("idle");
const prowlarrValidationMessage = ref("");
const tmdbDraft = ref("");
const p115CookieDraft = ref("");
const tmdbSaving = ref(false);
const tmdbResetting = ref(false);
const p115CredentialSaving = ref(false);
const p115CredentialResetting = ref(false);
const tmdbCredentialError = ref("");
const p115CredentialError = ref("");
const p115Devices = ref<P115LoginDevice[]>([]);
const p115DevicesLoading = ref(true);
const p115DevicesError = ref("");
const p115QrImage = ref("");
const p115QrSessionId = ref("");
const p115QrStatus = ref<"idle" | "waiting" | "scanned" | "ready" | "expired" | "error">("idle");
const p115QrDeviceCode = ref("web");
const p115QrDeviceName = ref("这台电脑");
const p115QrError = ref("");
const p115QrBusy = ref(false);
let p115QrPollTimer: number | null = null;
const credentialMutationBusy = computed(() => tmdbSaving.value || tmdbResetting.value || p115CredentialSaving.value || p115CredentialResetting.value);
const p115MutationBusy = computed(() => p115CredentialSaving.value || p115CredentialResetting.value);
const prowlarrMutationBusy = computed(() => prowlarrSaving.value || prowlarrResetting.value);
const inspectionSettings = ref<InspectionSettingsResponse | null>(null);
const inspectionLoading = ref(true);
const inspectionError = ref("");
const inspectionDraft = ref(true);
const savingInspection = ref(false);
const inspectionSaveError = ref("");
const inspectionConflict = ref(false);
const validationState = ref<ValidationState>("idle");
const validationMessage = ref("");
const organizationSettings = ref<OrganizationSettingsResponse | null>(null);
const organizationLoading = ref(true);
const organizationError = ref("");
const organizationSaveError = ref("");
const organizationSaving = ref(false);
const organizationSourceDraft = ref("");
const organizationSourceLabelsDraft = ref<string[]>([]);
const organizationTargetDraft = ref("");
const organizationTargetLabelDraft = ref("");
const organizationPushDraft = ref("");
const organizationPushLabelDraft = ref("");
const organizationVideoExtensionsDraft = ref("");
const organizationMetadataExtensionsDraft = ref("");
const directoryPickerOpen = ref(false);
const directoryPickerMode = ref<"source" | "target" | "push">("source");
const directoryPickerLoading = ref(false);
const directoryPickerError = ref("");
const directoryPickerItems = ref<P115DirectoryItem[]>([]);
const directoryPickerCurrentId = ref("");
const directoryPickerCurrentName = ref("115 网盘根目录");
const directoryPickerCurrentPath = ref("");
const directoryPickerTrail = ref<Array<P115DirectoryItem & { relative_path: string }>>([]);
const organizationDraft = ref({
  schedule_enabled: false,
  auto_execute_enabled: false,
  scan_interval_minutes: 30,
  rename_enabled: true,
  media_probe_enabled: true,
  ai_identification_enabled: false,
  small_file_threshold_mb: 0,
  cleanup_empty_directories: false,
  strm_linkage_enabled: false,
  operation_delay_seconds: 1.5,
  include_children_category: false,
  include_concert_category: false,
  region_grouping_enabled: true,
  year_grouping_enabled: false,
  prefer_remux: true,
  prefer_resolution: true,
  prefer_dolby: false,
  conflict_mode: 2 as 0 | 1 | 2,
  multi_version_enabled: false,
});

const logging = ref<LoggingSettingsResponse | null>(null);
const loggingLoading = ref(true);
const loggingError = ref("");
const draftLevel = ref<LogLevel>("INFO");
const draftRetentionDays = ref(30);
const draftMaxFileMb = ref(10);
const savingLogging = ref(false);
const saveError = ref("");
const conflict = ref(false);
let settingsMounted = false;

const contentPolicy = ref<ContentPolicyResponse | null>(null);
const contentLoading = ref(false);
const contentError = ref("");
const contentSaving = ref(false);
const contentSaveError = ref("");
const contentConflict = ref(false);
const contentDraftHideAdultMedia = ref(true);
const contentDraftHideSuspiciousResources = ref(true);
const contentDraftHideLowQualityResources = ref(true);
const blockedKeywordsDraft = ref("");

const loggingDirty = computed(() => {
  if (!logging.value) return false;
  return logging.value.level !== draftLevel.value
    || logging.value.retention_days !== draftRetentionDays.value
    || logging.value.max_file_mb !== draftMaxFileMb.value;
});
const currentSection = computed(() => sections.find((section) => section.id === activeSection.value));

function selectSection(section: SettingsSection) {
  activeSection.value = section;
  if (section === "credentials" && !credentials.value && !credentialsLoading.value) void loadCredentials();
  if (section === "prowlarr" && !prowlarr.value && !prowlarrLoading.value) void loadProwlarr();
  if (section === "content" && !contentPolicy.value) void loadContentPolicy();
  if (section === "organization" && !organizationSettings.value && !organizationLoading.value) void loadOrganization();
}

function selectMobileSection(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (value === "overview" || value === "credentials" || value === "prowlarr" || value === "logs" || value === "content" || value === "inspection" || value === "organization") selectSection(value);
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
    const response = await props.api.p115Settings();
    if (!settingsMounted) return;
    p115.value = response;
  } catch (exception) {
    if (settingsMounted) p115Error.value = exception instanceof ApiError ? exception.message : "115 状态加载失败，请稍后重试";
  } finally {
    if (settingsMounted) p115Loading.value = false;
  }
}

async function loadProwlarr() {
  prowlarrLoading.value = true;
  prowlarrError.value = "";
  prowlarrSaveError.value = "";
  prowlarrConflict.value = false;
  try {
    const response = await props.api.prowlarrSettings();
    if (!settingsMounted) return;
    applyProwlarr(response);
  } catch (exception) {
    if (settingsMounted) prowlarrError.value = exception instanceof ApiError ? exception.message : "Prowlarr 状态加载失败，请稍后重试";
  } finally {
    if (settingsMounted) prowlarrLoading.value = false;
  }
}

function applyProwlarr(value: ProwlarrSettingsResponse) {
  prowlarr.value = value;
  prowlarrBaseUrlDraft.value = value.base_url ?? "";
  prowlarrEnabledDraft.value = value.enabled;
  prowlarrApiKeyDraft.value = "";
}

function prowlarrMutationError(exception: unknown, fallback: string): string {
  if (!(exception instanceof ApiError)) return fallback;
  if (exception.status === 409) return "设置已被其他请求修改，请重新加载后再保存。";
  if (exception.status === 422) return "Prowlarr 地址或配置格式不正确，请检查后重试。";
  if (exception.status === 429) return "操作过于频繁，请稍后重试。";
  if (exception.status === 503) return "Prowlarr 设置服务暂不可用，请稍后重试。";
  return exception.message || fallback;
}

async function saveProwlarr() {
  if (!prowlarr.value || prowlarrMutationBusy.value || prowlarrValidationState.value === "running") return;
  prowlarrSaving.value = true;
  prowlarrSaveError.value = "";
  prowlarrSaveMessage.value = "";
  prowlarrConflict.value = false;
  const payload: PatchProwlarrSettingsRequest = {
    enabled: prowlarrEnabledDraft.value,
    base_url: prowlarrBaseUrlDraft.value.trim() || null,
    revision: prowlarr.value.revision,
  };
  if (prowlarrApiKeyDraft.value) payload.api_key = prowlarrApiKeyDraft.value;
  try {
    const response = await props.api.updateProwlarrSettings(payload);
    if (!settingsMounted) return;
    applyProwlarr(response);
    syncSharedRevision(response.revision);
    prowlarrSaveMessage.value = "Prowlarr 配置已保存";
  } catch (exception) {
    if (!settingsMounted) return;
    prowlarrConflict.value = isConflict(exception);
    prowlarrSaveError.value = prowlarrMutationError(exception, "Prowlarr 配置保存失败，请稍后重试");
  } finally {
    if (settingsMounted) prowlarrSaving.value = false;
  }
}

async function resetProwlarr() {
  if (!prowlarr.value || prowlarrMutationBusy.value || prowlarrValidationState.value === "running" || prowlarr.value.source !== "managed") return;
  prowlarrResetting.value = true;
  prowlarrSaveError.value = "";
  prowlarrSaveMessage.value = "";
  prowlarrConflict.value = false;
  try {
    const response = await props.api.resetProwlarrSettings(prowlarr.value.revision);
    if (!settingsMounted) return;
    applyProwlarr(response);
    syncSharedRevision(response.revision);
    prowlarrSaveMessage.value = "已恢复环境配置";
  } catch (exception) {
    if (!settingsMounted) return;
    prowlarrConflict.value = isConflict(exception);
    prowlarrSaveError.value = prowlarrMutationError(exception, "Prowlarr 配置恢复失败，请稍后重试");
  } finally {
    if (settingsMounted) prowlarrResetting.value = false;
  }
}

async function loadP115Devices() {
  p115DevicesLoading.value = true;
  p115DevicesError.value = "";
  try {
    const response = await props.api.p115Devices();
    if (!settingsMounted) return;
    p115Devices.value = response.items ?? [];
  } catch (exception) {
    if (settingsMounted) {
      p115DevicesError.value = exception instanceof ApiError ? exception.message : "扫码设备加载失败，请稍后重试";
    }
  } finally {
    if (settingsMounted) p115DevicesLoading.value = false;
  }
}

function stopP115QrPolling() {
  if (p115QrPollTimer !== null) {
    window.clearTimeout(p115QrPollTimer);
    p115QrPollTimer = null;
  }
}

async function startP115QrLogin() {
  stopP115QrPolling();
  p115QrBusy.value = true;
  p115QrError.value = "";
  p115QrStatus.value = "idle";
  try {
    const response = await props.api.createP115Qrcode(p115QrDeviceCode.value, p115QrDeviceName.value.trim() || "这台电脑");
    p115QrImage.value = response.image_data_url;
    p115QrSessionId.value = response.session_id;
    p115QrStatus.value = "waiting";
    await pollP115QrLogin();
  } catch (exception) {
    p115QrStatus.value = "error";
    p115QrError.value = exception instanceof ApiError ? exception.message : "二维码生成失败，请稍后重试";
    p115QrBusy.value = false;
  }
}

async function pollP115QrLogin() {
  if (!p115QrSessionId.value) return;
  try {
    const response = await props.api.pollP115Qrcode(p115QrSessionId.value);
    p115QrStatus.value = response.status;
    if (response.status === "ready") {
      p115QrBusy.value = false;
      p115QrError.value = "扫码设备已保存，并已切换为当前设备。";
      await Promise.all([loadCredentials(), loadP115(), loadP115Devices()]);
      return;
    }
    if (response.status === "expired") {
      p115QrBusy.value = false;
      p115QrError.value = "二维码已过期，请重新生成。";
      return;
    }
    p115QrPollTimer = window.setTimeout(() => void pollP115QrLogin(), 2000);
  } catch (exception) {
    if (exception instanceof ApiError && exception.retryable && p115QrSessionId.value) {
      p115QrStatus.value = "waiting";
      p115QrError.value = "二维码状态暂时未返回，正在重试。";
      p115QrPollTimer = window.setTimeout(() => void pollP115QrLogin(), 2000);
      return;
    }
    p115QrBusy.value = false;
    p115QrStatus.value = "error";
    p115QrError.value = exception instanceof ApiError ? exception.message : "二维码状态查询失败，请稍后重试";
  }
}

async function activateP115Device(device: P115LoginDevice) {
  if (!credentials.value || device.active || p115QrBusy.value) return;
  p115QrError.value = "";
  p115DevicesError.value = "";
  try {
    p115Devices.value = (await props.api.activateP115Device(device.id, credentials.value.revision)).items ?? [];
    await Promise.all([loadCredentials(), loadP115()]);
    p115QrError.value = `已切换到“${device.name}”，其他设备仍保留。`;
  } catch (exception) {
    p115QrError.value = exception instanceof ApiError ? exception.message : "设备切换失败，请稍后重试";
  }
}

async function revokeP115Device(device: P115LoginDevice) {
  if (device.active || p115QrBusy.value) return;
  p115DevicesError.value = "";
  try {
    await props.api.revokeP115Device(device.id);
    await loadP115Devices();
  } catch (exception) {
    p115QrError.value = exception instanceof ApiError ? exception.message : "设备移除失败，请稍后重试";
  }
}

function applyCredentials(value: CredentialSettingsResponse) {
  credentials.value = value;
}

async function loadCredentials() {
  credentialsLoading.value = true;
  credentialsError.value = "";
  tmdbCredentialError.value = "";
  p115CredentialError.value = "";
  try {
    const response = await props.api.credentialSettings();
    if (!settingsMounted) return;
    applyCredentials(response);
  } catch (exception) {
    if (settingsMounted) credentialsError.value = credentialErrorMessage(exception, "连接配置加载失败，请稍后重试");
  } finally {
    credentialsLoading.value = false;
  }
}

function credentialErrorMessage(exception: unknown, fallback: string): string {
  if (!(exception instanceof ApiError)) return fallback;
  if (exception.status === 409) return "设置已被其他请求修改，请重新加载后再试。";
  if (exception.status === 422) return "凭据格式或验证未通过，请检查后重试。";
  if (exception.status === 429) return "操作过于频繁，请稍后重试。";
  if (exception.status === 503) return "凭据服务暂不可用，请稍后重试。";
  return fallback;
}

async function saveTmdbCredential() {
  if (!credentials.value || credentialMutationBusy.value) return;
  if (!tmdbDraft.value) {
    tmdbCredentialError.value = "请输入 TMDB API Key。";
    return;
  }
  tmdbSaving.value = true;
  tmdbCredentialError.value = "";
  try {
    const response = await props.api.updateTmdbCredential(tmdbDraft.value, credentials.value.revision);
    if (!settingsMounted) return;
    applyCredentials(response);
    syncSharedRevision(response.revision);
    tmdbDraft.value = "";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (settingsMounted) tmdbCredentialError.value = credentialErrorMessage(exception, "TMDB 凭据保存失败，请稍后重试");
  } finally {
    if (settingsMounted) tmdbSaving.value = false;
  }
}

async function resetTmdbCredential() {
  if (!credentials.value || credentialMutationBusy.value || credentials.value.tmdb.source !== "managed") return;
  tmdbResetting.value = true;
  tmdbCredentialError.value = "";
  try {
    const response = await props.api.resetTmdbCredential(credentials.value.revision);
    if (!settingsMounted) return;
    applyCredentials(response);
    syncSharedRevision(response.revision);
    tmdbDraft.value = "";
  } catch (exception) {
    focusFirstFieldError(exception);
    if (settingsMounted) tmdbCredentialError.value = credentialErrorMessage(exception, "TMDB 配置恢复失败，请稍后重试");
  } finally {
    if (settingsMounted) tmdbResetting.value = false;
  }
}

async function saveP115Credential() {
  if (!credentials.value || credentialMutationBusy.value || validationState.value === "running") return;
  if (!p115CookieDraft.value) {
    p115CredentialError.value = "请输入 P115 Cookie。";
    return;
  }
  p115CredentialSaving.value = true;
  p115CredentialError.value = "";
  try {
    const response = await props.api.updateP115Cookie(p115CookieDraft.value, credentials.value.revision);
    if (!settingsMounted) return;
    applyCredentials(response);
    syncSharedRevision(response.revision);
    p115CookieDraft.value = "";
    await loadP115();
  } catch (exception) {
    focusFirstFieldError(exception);
    if (settingsMounted) p115CredentialError.value = credentialErrorMessage(exception, "P115 Cookie 保存失败，请稍后重试");
  } finally {
    if (settingsMounted) p115CredentialSaving.value = false;
  }
}

async function resetP115Credential() {
  if (!credentials.value || credentialMutationBusy.value || validationState.value === "running" || credentials.value.p115_cookie.source !== "managed") return;
  p115CredentialResetting.value = true;
  p115CredentialError.value = "";
  try {
    const response = await props.api.resetP115Cookie(credentials.value.revision);
    if (!settingsMounted) return;
    applyCredentials(response);
    syncSharedRevision(response.revision);
    p115CookieDraft.value = "";
    await loadP115();
  } catch (exception) {
    focusFirstFieldError(exception);
    if (settingsMounted) p115CredentialError.value = credentialErrorMessage(exception, "P115 Cookie 恢复失败，请稍后重试");
  } finally {
    if (settingsMounted) p115CredentialResetting.value = false;
  }
}

function applyInspection(value: InspectionSettingsResponse) {
  inspectionSettings.value = value;
  inspectionDraft.value = value.auto_start_enabled;
}

async function loadInspection() {
  inspectionLoading.value = true;
  inspectionError.value = "";
  inspectionSaveError.value = "";
  inspectionConflict.value = false;
  try {
    applyInspection(await props.api.inspectionSettings());
  } catch (exception) {
    inspectionError.value = exception instanceof ApiError ? exception.message : "资源检测设置加载失败，请稍后重试";
  } finally {
    inspectionLoading.value = false;
  }
}

const inspectionDirty = computed(() => Boolean(inspectionSettings.value) && inspectionDraft.value !== inspectionSettings.value?.auto_start_enabled);

async function saveInspection() {
  if (!inspectionSettings.value || savingInspection.value || !inspectionDirty.value) return;
  savingInspection.value = true;
  inspectionSaveError.value = "";
  inspectionConflict.value = false;
  try {
    const response = await props.api.updateInspectionSettings({
      auto_start_enabled: inspectionDraft.value,
      revision: inspectionSettings.value.revision,
    });
    applyInspection(response);
    syncSharedRevision(response.revision);
    emit("auto-start-enabled", response.auto_start_enabled);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      inspectionConflict.value = true;
      inspectionSaveError.value = CONFLICT_MESSAGE_ZH;
    } else {
      inspectionSaveError.value = exception instanceof ApiError ? exception.message : "保存失败，请稍后重试";
    }
  } finally {
    savingInspection.value = false;
  }
}

function applyOrganization(value: OrganizationSettingsResponse) {
  organizationSettings.value = value;
  organizationDraft.value = {
    schedule_enabled: value.schedule_enabled,
    auto_execute_enabled: value.auto_execute_enabled,
    scan_interval_minutes: value.scan_interval_minutes,
    rename_enabled: value.rename_enabled,
    media_probe_enabled: value.media_probe_enabled,
    ai_identification_enabled: value.ai_identification_enabled,
    small_file_threshold_mb: value.small_file_threshold_mb,
    cleanup_empty_directories: value.cleanup_empty_directories,
    strm_linkage_enabled: value.strm_linkage_enabled,
    operation_delay_seconds: value.operation_delay_seconds,
    include_children_category: value.include_children_category,
    include_concert_category: value.include_concert_category,
    region_grouping_enabled: value.region_grouping_enabled,
    year_grouping_enabled: value.year_grouping_enabled,
    prefer_remux: value.prefer_remux,
    prefer_resolution: value.prefer_resolution,
    prefer_dolby: value.prefer_dolby,
    conflict_mode: value.conflict_mode,
    multi_version_enabled: value.multi_version_enabled,
  };
  organizationSourceDraft.value = value.source_directory_ids.join(", ");
  organizationSourceLabelsDraft.value = (value.source_directory_labels ?? []).slice(0, value.source_directory_ids.length);
  organizationTargetDraft.value = value.target_directory_id ?? "";
  organizationTargetLabelDraft.value = value.target_directory_label ?? "";
  organizationPushDraft.value = value.push_directory_id ?? "";
  organizationPushLabelDraft.value = value.push_directory_label ?? "";
  organizationVideoExtensionsDraft.value = value.video_extensions.join(", ");
  organizationMetadataExtensionsDraft.value = value.metadata_extensions.join(", ");
}

async function loadOrganization() {
  organizationLoading.value = true;
  organizationError.value = "";
  organizationSaveError.value = "";
  try {
    applyOrganization(await props.api.organizationSettings());
  } catch (exception) {
    organizationError.value = exception instanceof ApiError ? exception.message : "整理设置加载失败，请稍后重试";
  } finally {
    organizationLoading.value = false;
  }
}

async function openDirectoryPicker(mode: "source" | "target" | "push") {
  directoryPickerMode.value = mode;
  directoryPickerOpen.value = true;
  directoryPickerTrail.value = [];
  directoryPickerCurrentId.value = "";
  directoryPickerCurrentName.value = "115 网盘根目录";
  directoryPickerCurrentPath.value = "";
  await loadDirectoryPicker();
}

async function loadDirectoryPicker() {
  directoryPickerLoading.value = true;
  directoryPickerError.value = "";
  try {
    const response = await props.api.p115Directories(directoryPickerCurrentId.value || undefined);
    directoryPickerCurrentId.value = response.parent_id;
    directoryPickerItems.value = response.items;
  } catch (exception) {
    directoryPickerError.value = exception instanceof ApiError ? exception.message : "目录读取失败，请检查 115 登录状态";
  } finally {
    directoryPickerLoading.value = false;
  }
}

async function enterDirectory(item: P115DirectoryItem) {
  directoryPickerTrail.value.push({ id: directoryPickerCurrentId.value, name: directoryPickerCurrentName.value, relative_path: directoryPickerCurrentPath.value });
  directoryPickerCurrentId.value = item.id;
  directoryPickerCurrentName.value = item.name;
  directoryPickerCurrentPath.value = [directoryPickerCurrentPath.value, safeDirectorySegment(item.name)].filter(Boolean).join("/");
  await loadDirectoryPicker();
}

async function leaveDirectory() {
  const previous = directoryPickerTrail.value.pop();
  if (!previous) return;
  directoryPickerCurrentId.value = previous.id;
  directoryPickerCurrentName.value = previous.name;
  directoryPickerCurrentPath.value = previous.relative_path;
  await loadDirectoryPicker();
}

function chooseDirectory() {
  if (!directoryPickerCurrentId.value) return;
  const label = directoryPickerCurrentPath.value || safeDirectorySegment(directoryPickerCurrentName.value);
  if (directoryPickerMode.value === "target") {
    organizationTargetDraft.value = directoryPickerCurrentId.value;
    organizationTargetLabelDraft.value = label;
  } else if (directoryPickerMode.value === "push") {
    organizationPushDraft.value = directoryPickerCurrentId.value;
    organizationPushLabelDraft.value = label;
  } else {
    const ids = organizationList(organizationSourceDraft.value);
    if (!ids.includes(directoryPickerCurrentId.value)) {
      ids.push(directoryPickerCurrentId.value);
      organizationSourceLabelsDraft.value.push(label);
    }
    organizationSourceDraft.value = ids.join(", ");
  }
  directoryPickerOpen.value = false;
}

function safeDirectorySegment(value: string) {
  return value.replace(/[\\/:*?"<>|\r\n]/g, " ").replace(/\s+/g, " ").trim() || "未命名目录";
}

function sourceDirectoryDisplayLabel(index: number) {
  return organizationSourceLabelsDraft.value[index] || "目录名称待确认";
}

function removeOrganizationSource(index: number) {
  const ids = organizationList(organizationSourceDraft.value);
  ids.splice(index, 1);
  organizationSourceLabelsDraft.value.splice(index, 1);
  organizationSourceDraft.value = ids.join(", ");
}

const organizationDirty = computed(() => {
  return organizationDraftChanged();
});

function organizationList(value: string) {
  return value.split(/[,，\s]+/).map((item) => item.trim().toLowerCase()).filter(Boolean);
}

function organizationDraftChanged() {
  if (!organizationSettings.value) return false;
  const draft = organizationDraft.value;
  return organizationSettings.value.schedule_enabled !== draft.schedule_enabled
    || organizationSettings.value.scan_interval_minutes !== draft.scan_interval_minutes
    || organizationSettings.value.rename_enabled !== draft.rename_enabled
    || organizationSettings.value.media_probe_enabled !== draft.media_probe_enabled
    || organizationSettings.value.ai_identification_enabled !== draft.ai_identification_enabled
    || organizationSettings.value.small_file_threshold_mb !== draft.small_file_threshold_mb
    || organizationSettings.value.cleanup_empty_directories !== draft.cleanup_empty_directories
    || organizationSettings.value.strm_linkage_enabled !== draft.strm_linkage_enabled
    || organizationSettings.value.operation_delay_seconds !== draft.operation_delay_seconds
    || organizationSettings.value.include_children_category !== draft.include_children_category
    || organizationSettings.value.include_concert_category !== draft.include_concert_category
    || organizationSettings.value.region_grouping_enabled !== draft.region_grouping_enabled
    || organizationSettings.value.year_grouping_enabled !== draft.year_grouping_enabled
    || organizationSettings.value.prefer_remux !== draft.prefer_remux
    || organizationSettings.value.prefer_resolution !== draft.prefer_resolution
    || organizationSettings.value.prefer_dolby !== draft.prefer_dolby
    || organizationSettings.value.conflict_mode !== draft.conflict_mode
    || organizationSettings.value.multi_version_enabled !== draft.multi_version_enabled
    || organizationSettings.value.target_directory_id !== (organizationTargetDraft.value.trim() || null)
    || (organizationSettings.value.target_directory_label ?? "") !== (organizationTargetLabelDraft.value.trim() || "")
    || organizationSettings.value.push_directory_id !== (organizationPushDraft.value.trim() || null)
    || (organizationSettings.value.push_directory_label ?? "") !== (organizationPushLabelDraft.value.trim() || "")
    || organizationSettings.value.source_directory_ids.join(",") !== organizationList(organizationSourceDraft.value).join(",")
    || (organizationSettings.value.source_directory_labels ?? []).join(",") !== organizationSourceLabelsDraft.value.join(",")
    || organizationSettings.value.video_extensions.join(",") !== organizationList(organizationVideoExtensionsDraft.value).join(",")
    || organizationSettings.value.metadata_extensions.join(",") !== organizationList(organizationMetadataExtensionsDraft.value).join(",");
}

async function saveOrganization() {
  if (!organizationSettings.value || organizationSaving.value || !organizationDraftChanged()) return;
  organizationSaving.value = true;
  organizationSaveError.value = "";
  try {
    const sourceDirectoryIds = organizationList(organizationSourceDraft.value);
    const response = await props.api.updateOrganizationSettings({
      ...organizationDraft.value,
      source_directory_ids: sourceDirectoryIds,
      source_directory_labels: sourceDirectoryIds.map((_, index) => organizationSourceLabelsDraft.value[index] ?? ""),
      target_directory_id: organizationTargetDraft.value.trim() || null,
      target_directory_label: organizationTargetDraft.value.trim() ? organizationTargetLabelDraft.value.trim() || null : null,
      push_directory_id: organizationPushDraft.value.trim() || null,
      push_directory_label: organizationPushDraft.value.trim() ? organizationPushLabelDraft.value.trim() || null : null,
      video_extensions: organizationList(organizationVideoExtensionsDraft.value),
      metadata_extensions: organizationList(organizationMetadataExtensionsDraft.value),
      revision: organizationSettings.value.revision,
    });
    applyOrganization(response);
    syncSharedRevision(response.revision);
  } catch (exception) {
    focusFirstFieldError(exception);
    organizationSaveError.value = exception instanceof ApiError ? exception.message : "整理设置保存失败，请稍后重试";
  } finally {
    organizationSaving.value = false;
  }
}

function applyLogging(value: LoggingSettingsResponse) {
  logging.value = value;
  draftLevel.value = value.level;
  draftRetentionDays.value = value.retention_days;
  draftMaxFileMb.value = value.max_file_mb;
}

async function loadLogging() {
  loggingLoading.value = true;
  loggingError.value = "";
  saveError.value = "";
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
    const response = await props.api.updateLoggingSettings({
      revision: logging.value.revision,
      level: draftLevel.value,
      retention_days: draftRetentionDays.value,
      max_file_mb: draftMaxFileMb.value,
    });
    applyLogging(response);
    syncSharedRevision(response.revision);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      conflict.value = true;
      saveError.value = CONFLICT_MESSAGE_ZH;
    } else {
      saveError.value = exception instanceof ApiError ? exception.message : "保存失败，请稍后重试";
    }
  } finally {
    savingLogging.value = false;
  }
}

async function loadContentPolicy() {
  contentLoading.value = true;
  contentError.value = "";
  try {
    applyContentPolicy(await props.api.contentPolicy());
  } catch (exception) {
    contentError.value = exception instanceof ApiError ? exception.message : "内容安全设置加载失败，请稍后重试";
  } finally {
    contentLoading.value = false;
  }
}

function applyContentPolicy(value: ContentPolicyResponse) {
  contentPolicy.value = value;
  contentDraftHideAdultMedia.value = value.hide_adult_media;
  contentDraftHideSuspiciousResources.value = value.hide_suspicious_resources;
  contentDraftHideLowQualityResources.value = value.hide_low_quality_resources;
  blockedKeywordsDraft.value = value.blocked_keywords.join("\n");
}

function openLogsPage(): void {
  emit("navigate", "logs");
}

function openOrganizationPage(): void {
  emit("navigate", "organization");
}

function syncSharedRevision(revision: number) {
  if (logging.value) logging.value = { ...logging.value, revision };
  if (inspectionSettings.value) inspectionSettings.value = { ...inspectionSettings.value, revision };
  if (contentPolicy.value) contentPolicy.value = { ...contentPolicy.value, revision };
  if (credentials.value) credentials.value = { ...credentials.value, revision };
  if (prowlarr.value) prowlarr.value = { ...prowlarr.value, revision };
  if (organizationSettings.value) organizationSettings.value = { ...organizationSettings.value, revision };
}

async function saveContentPolicy() {
  if (!contentPolicy.value || contentSaving.value) return;
  contentSaving.value = true;
  contentSaveError.value = "";
  contentConflict.value = false;
  try {
    const response = await props.api.updateContentPolicy({
      revision: contentPolicy.value.revision,
      hide_adult_media: contentDraftHideAdultMedia.value,
      hide_suspicious_resources: contentDraftHideSuspiciousResources.value,
      hide_low_quality_resources: contentDraftHideLowQualityResources.value,
      blocked_keywords: blockedKeywordsDraft.value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean),
    });
    applyContentPolicy(response);
    syncSharedRevision(response.revision);
  } catch (exception) {
    focusFirstFieldError(exception);
    if (isConflict(exception)) {
      contentConflict.value = true;
      contentSaveError.value = CONFLICT_MESSAGE_ZH;
    } else {
      contentSaveError.value = exception instanceof ApiError ? exception.message : "保存失败，请稍后重试";
    }
  } finally {
    contentSaving.value = false;
  }
}

async function validateP115() {
  if (validationState.value === "running" || p115MutationBusy.value) return;
  validationState.value = "running";
  validationMessage.value = "正在验证已配置 Cookie";
  try {
    const response = await props.api.validateP115Cookie();
    if (!settingsMounted) return;
    validationState.value = response.status;
    validationMessage.value = `${validationLabel(response.status)}（${formatTimestamp(response.checked_at)}）`;
    await loadP115();
  } catch (exception) {
    if (!settingsMounted) return;
    validationState.value = "error";
    validationMessage.value = exception instanceof ApiError ? exception.message : "Cookie 验证失败，请稍后重试";
  }
}

async function validateProwlarr() {
  if (prowlarrValidationState.value === "running" || prowlarrMutationBusy.value) return;
  prowlarrValidationState.value = "running";
  prowlarrValidationMessage.value = "正在验证 Prowlarr 连接";
  try {
    const response = await props.api.verifyProwlarr();
    if (!settingsMounted) return;
    prowlarrValidationState.value = response.status;
    prowlarrValidationMessage.value = `${prowlarrVerificationMessage(response)}（${formatTimestamp(response.checked_at)}）`;
  } catch (exception) {
    if (!settingsMounted) return;
    prowlarrValidationState.value = "error";
    prowlarrValidationMessage.value = exception instanceof ApiError ? exception.message : "Prowlarr 连接验证失败，请稍后重试";
  }
}

function formatBytes(value: number) {
  if (!Number.isFinite(value)) return "未知";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatUptime(value: number) {
  if (!Number.isFinite(value)) return "未知";
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return days ? `${days} 天 ${hours} 小时` : `${hours} 小时 ${minutes} 分钟`;
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString("zh-CN", { hour12: false });
}

function shortRelease(value: string | null | undefined) {
  const normalized = value?.trim() ?? "";
  if (normalized.length <= 12) return normalized || "未知";
  return `${normalized.slice(0, 7)}…`;
}

function redactDirectoryId(value: string | null | undefined) {
  const normalized = value?.trim() ?? "";
  if (!normalized) return "未配置";
  if (normalized.length < 6) return `已配置（${normalized.length}位）`;
  return `${normalized.slice(0, 3)}…${normalized.slice(-2)}`;
}

function redactedDirectoryIds(value: string) {
  const ids = organizationList(value);
  return ids.length ? ids.map((item) => redactDirectoryId(item)).join("、") : "未配置";
}

function overviewCapabilityDetail(key: string): CapabilityAvailability | null {
  return overview.value?.capability_details?.[key] ?? null;
}

function capabilityLabel(value: boolean) {
  return value ? "可用" : "不可用";
}

function capabilityClass(value: boolean) {
  return value ? "status-ok" : "status-degraded";
}

type OverviewCapabilityKey = keyof SettingsOverviewResponse["capabilities"];
type CapabilityPresentation = {
  label: string;
  className: string;
  nextStep: string;
};

const readOnlyCapabilityKeys = new Set<OverviewCapabilityKey>(["inspection", "strm_playback"]);
const contractCapabilityKeys = new Set<OverviewCapabilityKey>(["organization_write", "permanent_delete", "strm_playback"]);
const overviewCapabilityGroups: Record<"basic" | "organization" | "strm", Array<{ key: OverviewCapabilityKey; label: string }>> = {
  basic: [
    { key: "inspection", label: "内容检测" },
    { key: "magnet", label: "磁力云下载" },
    { key: "share", label: "115 分享转存" },
  ],
  organization: [
    { key: "organization_plan", label: "整理计划" },
    { key: "organization_execution", label: "整理执行" },
    { key: "organization_write", label: "移动与重命名" },
    { key: "organization_empty_directory_cleanup", label: "空目录回收" },
    { key: "permanent_delete", label: "永久删除" },
  ],
  strm: [
    { key: "strm_full", label: "全量生成" },
    { key: "strm_incremental", label: "增量同步" },
    { key: "strm_cleanup", label: "失效清理" },
    { key: "strm_playback", label: "动态播放" },
  ],
};

function overviewCapability(key: OverviewCapabilityKey, fallback: boolean): CapabilityStatusResponse {
  return overview.value?.capability_statuses?.[key] ?? {
    state: fallback ? "runtime_healthy" : "unconfigured",
    state_zh: fallback ? "可用" : "未启用",
    last_success_at: null,
  };
}

function overviewCapabilityPresentation(key: OverviewCapabilityKey, fallback: boolean): CapabilityPresentation {
  const status = overviewCapability(key, fallback);
  const enabled = overview.value?.capabilities?.[key] ?? fallback;
  const detail = overviewCapabilityDetail(key);
  if (enabled) {
    return {
      label: readOnlyCapabilityKeys.has(key) ? "只读可用" : "可执行",
      className: "status-ok",
      nextStep: detail?.reason_zh === "可用" ? "" : detail?.reason_zh ?? "",
    };
  }
  if (status.state === "unconfigured") {
    return {
      label: "未配置",
      className: "status-degraded",
      nextStep: detail?.reason_zh ?? "请先完成对应连接、目录或凭据配置。",
    };
  }
  if (contractCapabilityKeys.has(key) && status.state === "configured") {
    return {
      label: "契约未验证",
      className: "status-degraded",
      nextStep: detail?.reason_zh ?? "远端契约未验证，写操作保持禁用。",
    };
  }
  return {
    label: "已配置但关闭",
    className: "status-degraded",
    nextStep: detail?.reason_zh ?? "请检查对应功能开关。",
  };
}

function overviewCapabilityClass(key: OverviewCapabilityKey, fallback: boolean) {
  return overviewCapabilityPresentation(key, fallback).className;
}

function overviewCapabilityLabel(key: OverviewCapabilityKey, fallback: boolean) {
  return overviewCapabilityPresentation(key, fallback).label;
}

function overviewCapabilityNextStep(key: OverviewCapabilityKey, fallback: boolean) {
  return overviewCapabilityPresentation(key, fallback).nextStep;
}

function overviewCapabilityAction(key: OverviewCapabilityKey): { label: string; section: SettingsSection } | null {
  if (key === "inspection") return { label: "打开检测设置", section: "inspection" };
  if (key === "magnet" || key === "share") return { label: "打开 115 连接配置", section: "credentials" };
  if (key === "organization_plan" || key === "organization_execution" || key === "organization_write" || key === "organization_empty_directory_cleanup") {
    return { label: "打开整理设置", section: "organization" };
  }
  return null;
}

function overviewCapabilityActionLabel(key: OverviewCapabilityKey) {
  return overviewCapabilityAction(key)?.label ?? "";
}

function openOverviewCapabilityAction(key: OverviewCapabilityKey) {
  const action = overviewCapabilityAction(key);
  if (action) selectSection(action.section);
}

function credentialSourceLabel(value: CredentialSettingsResponse["tmdb"]["source"] | CredentialSettingsResponse["p115_cookie"]["source"]) {
  return value === "managed" ? "托管" : value === "environment" ? "环境变量" : "文件";
}

function validationLabel(value: P115ValidationResponse["status"]) {
  return { ready: "Cookie 已就绪", needs_auth: "Cookie 需要重新授权", unavailable: "115 当前不可用" }[value];
}

function validationClass(value: ValidationState) {
  return value === "ready" ? "status-ok" : value === "needs_auth" ? "status-degraded" : "status-down";
}

type ProwlarrDisplayStatus = "configured" | "disabled" | "unavailable" | NonNullable<ProwlarrSettingsResponse["health_state"]>;

function prowlarrStatus(value: ProwlarrSettingsResponse): ProwlarrDisplayStatus {
  if (!value.enabled) return "disabled";
  if (!value.configured) return "unavailable";
  return value.health_state ?? "configured";
}

function prowlarrStatusLabel(value: ProwlarrSettingsResponse): string {
  return {
    configured: "已配置",
    disabled: "未启用",
    unavailable: "待配置",
    not_configured: "待配置",
    unverified: "配置未验证",
    available: "可用",
    degraded: "已降级",
    backoff: "暂时退避",
    open_circuit: "暂时熔断",
  }[prowlarrStatus(value)];
}

function prowlarrStatusClass(value: ProwlarrSettingsResponse): string {
  const status = prowlarrStatus(value);
  if (status === "configured" || status === "available") return "status-ok";
  if (status === "unavailable" || status === "not_configured") return "status-down";
  return "status-degraded";
}

function prowlarrHealthNote(value: ProwlarrSettingsResponse): string | null {
  if (!value.enabled || !value.configured) return null;
  const reason = value.health_reason_zh ?? value.health_message_zh;
  const retryAfter = value.health_retry_after_seconds;
  const retryMessage = typeof retryAfter === "number" && Number.isFinite(retryAfter) && retryAfter > 0
    ? `预计 ${Math.ceil(retryAfter)} 秒后重试`
    : null;
  return [reason, retryMessage].filter((item): item is string => Boolean(item)).join(" ") || null;
}

function prowlarrSourceLabel(value: ProwlarrSettingsResponse["source"]): string {
  return { managed: "页面配置", environment: "环境变量", none: "未配置" }[value];
}

function prowlarrApiKeySourceLabel(value: ProwlarrSettingsResponse["api_key_source"]): string {
  return { managed: "页面配置", environment: "环境变量", none: "未配置" }[value];
}

function prowlarrVerificationMessage(response: ProwlarrVerifyResponse): string {
  const codeLabels: Record<string, string> = {
    prowlarr_disabled: "Prowlarr 未启用",
    prowlarr_not_configured: "Prowlarr 尚未配置",
    prowlarr_auth_required: "Prowlarr 认证失败，请检查 API Key",
    prowlarr_invalid_response: "Prowlarr 返回格式不受支持",
    prowlarr_unavailable: "Prowlarr 当前不可用",
  };
  if (response.message_code && codeLabels[response.message_code]) return codeLabels[response.message_code];
  return response.status === "available" ? "连接验证成功" : response.status === "disabled" ? "Prowlarr 未启用" : "Prowlarr 当前不可用";
}

function prowlarrValidationClass(value: typeof prowlarrValidationState.value) {
  return value === "available" ? "status-ok" : value === "disabled" || value === "idle" || value === "running" ? "status-unknown" : "status-down";
}

onMounted(() => {
  settingsMounted = true;
  void loadOverview();
  void loadLogging();
  void loadInspection();
  void loadP115();
  void loadProwlarr();
  void loadP115Devices();
  void loadOrganization();
});

onBeforeUnmount(() => {
  settingsMounted = false;
  stopP115QrPolling();
});
</script>

<template>
  <section class="settings-view">
    <header class="settings-heading"><div><p class="eyebrow">工作区设置</p><h1>设置</h1><p>查看运行状态、日志策略和 115 连接配置。</p></div><Settings2 :size="28" /></header>
    <div class="settings-layout">
      <aside class="settings-nav" aria-label="设置分区"><div v-for="group in sectionGroups" :key="group.label" class="settings-nav-group"><p class="settings-nav-group-label">{{ group.label }}</p><button v-for="item in group.items" :key="item.id" type="button" :class="{ active: activeSection === item.id }" @click="selectSection(item.id)"><component :is="item.icon" :size="16" />{{ item.label }}</button></div></aside>
      <div class="settings-content">
        <label class="settings-mobile-select">分区<select aria-label="设置分区" :value="activeSection" @change="selectMobileSection"><optgroup v-for="group in sectionGroups" :key="group.label" :label="group.label"><option v-for="item in group.items" :key="item.id" :value="item.id">{{ item.label }}</option></optgroup></select></label>
        <div class="settings-section-kicker"><span>{{ currentSection?.label }}</span><span>观影助手</span></div>

        <section v-if="activeSection === 'overview'" class="settings-section" aria-labelledby="overview-title">
          <header class="settings-section-heading"><div><p class="eyebrow">系统概览</p><h2 id="overview-title">概览</h2></div><button class="icon-button" type="button" title="刷新概览" aria-label="刷新概览" :disabled="overviewLoading" @click="loadOverview"><RefreshCw :size="16" :class="{ spin: overviewLoading }" /></button></header>
          <div v-if="overviewLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载概览</div>
          <div v-else-if="overviewError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ overviewError }}</span><button class="text-button" type="button" @click="loadOverview">重试</button></div>
          <template v-else-if="overview">
            <div class="settings-metrics"><div class="settings-metric"><span>版本</span><strong>{{ shortRelease(overview.release) }}</strong></div><div class="settings-metric"><span>运行时间</span><strong>{{ formatUptime(overview.uptime_seconds) }}</strong></div><div class="settings-metric"><span>数据库大小</span><strong>{{ formatBytes(overview.database_size_bytes) }}</strong></div></div>
            <div class="settings-subsection"><h3>能力</h3><div class="settings-capability-list">
              <div v-for="entry in overviewCapabilityGroups.basic" :key="entry.key" class="settings-capability-row"><span>{{ entry.label }}<small v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false)">下一步：{{ overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) }}</small></span><span class="settings-capability-value"><strong :class="overviewCapabilityClass(entry.key, overview.capabilities?.[entry.key] ?? false)">{{ overviewCapabilityLabel(entry.key, overview.capabilities?.[entry.key] ?? false) }}</strong><button v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) && overviewCapabilityAction(entry.key)" class="text-button" type="button" @click="openOverviewCapabilityAction(entry.key)">{{ overviewCapabilityActionLabel(entry.key) }}</button></span></div>
            </div></div>
            <div class="settings-subsection"><h3>115 真实操作</h3><div class="settings-capability-list">
              <div v-for="entry in overviewCapabilityGroups.organization" :key="entry.key" class="settings-capability-row"><span>{{ entry.label }}<small v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false)">下一步：{{ overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) }}</small></span><span class="settings-capability-value"><strong :class="overviewCapabilityClass(entry.key, overview.capabilities?.[entry.key] ?? false)">{{ overviewCapabilityLabel(entry.key, overview.capabilities?.[entry.key] ?? false) }}</strong><button v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) && overviewCapabilityAction(entry.key)" class="text-button" type="button" @click="openOverviewCapabilityAction(entry.key)">{{ overviewCapabilityActionLabel(entry.key) }}</button></span></div>
            </div></div>
            <div class="settings-subsection"><h3>STRM</h3><div class="settings-capability-list">
              <div v-for="entry in overviewCapabilityGroups.strm" :key="entry.key" class="settings-capability-row"><span>{{ entry.label }}<small v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false)">下一步：{{ overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) }}</small></span><span class="settings-capability-value"><strong :class="overviewCapabilityClass(entry.key, overview.capabilities?.[entry.key] ?? false)">{{ overviewCapabilityLabel(entry.key, overview.capabilities?.[entry.key] ?? false) }}</strong><button v-if="overviewCapabilityNextStep(entry.key, overview.capabilities?.[entry.key] ?? false) && overviewCapabilityAction(entry.key)" class="text-button" type="button" @click="openOverviewCapabilityAction(entry.key)">{{ overviewCapabilityActionLabel(entry.key) }}</button></span></div>
            </div></div>
          </template>
        </section>

        <section v-else-if="activeSection === 'credentials'" class="settings-section" aria-labelledby="credentials-title">
          <header class="settings-section-heading"><div><p class="eyebrow">托管连接</p><h2 id="credentials-title">连接配置</h2></div><button class="icon-button" type="button" title="刷新连接配置" aria-label="刷新连接配置" :disabled="credentialsLoading" @click="loadCredentials"><RefreshCw :size="16" :class="{ spin: credentialsLoading }" /></button></header>
          <div v-if="credentialsLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载连接配置</div>
          <div v-else-if="credentialsError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ credentialsError }}</span><button class="text-button" type="button" @click="loadCredentials">重新加载</button></div>
          <div v-else-if="credentials" class="credential-panels">
            <section class="credential-panel" aria-labelledby="tmdb-credential-title">
              <header class="credential-panel-heading"><div><p class="eyebrow">TMDB</p><h3 id="tmdb-credential-title">TMDB API Key</h3></div><KeyRound :size="20" /></header>
              <dl class="credential-status-grid"><div><dt>是否配置</dt><dd :class="credentials.tmdb.configured ? 'status-ok' : 'status-degraded'">{{ credentials.tmdb.configured ? '已配置' : '未配置' }}</dd></div><div><dt>来源</dt><dd>{{ credentialSourceLabel(credentials.tmdb.source) }}</dd></div><div><dt>最后更新时间</dt><dd>{{ credentials.tmdb.last_updated_at ? formatTimestamp(credentials.tmdb.last_updated_at) : '未知' }}</dd></div></dl>
              <div class="credential-form"><label>API Key<input v-model="tmdbDraft" type="password" autocomplete="new-password" spellcheck="false" aria-label="TMDB API Key" placeholder="输入新的 API Key" :disabled="credentialMutationBusy" /></label><div class="credential-actions"><button class="primary-button" type="button" :disabled="credentialMutationBusy" @click="saveTmdbCredential"><LoaderCircle v-if="tmdbSaving" class="spin" :size="15" /><Save v-else :size="15" />保存并验证</button><button class="secondary-button" type="button" :disabled="credentialMutationBusy || credentials.tmdb.source !== 'managed'" @click="resetTmdbCredential"><LoaderCircle v-if="tmdbResetting" class="spin" :size="15" /><RefreshCw v-else :size="15" />恢复环境配置</button></div></div>
              <p v-if="tmdbCredentialError" class="settings-state settings-state-error credential-error" role="alert"><AlertTriangle :size="16" />{{ tmdbCredentialError }}<button v-if="tmdbCredentialError.includes('其他请求')" class="text-button" type="button" @click="loadCredentials">重新加载</button></p>
            </section>

            <section class="credential-panel" aria-labelledby="p115-credential-title">
              <header class="credential-panel-heading"><div><p class="eyebrow">P115</p><h3 id="p115-credential-title">P115 Cookie</h3></div><Cookie :size="20" /></header>
              <dl class="credential-status-grid"><div><dt>是否配置</dt><dd :class="credentials.p115_cookie.configured ? 'status-ok' : 'status-degraded'">{{ credentials.p115_cookie.configured ? '已配置' : '未配置' }}</dd></div><div><dt>来源</dt><dd>{{ credentialSourceLabel(credentials.p115_cookie.source) }}</dd></div><div><dt>结构状态</dt><dd :class="credentials.p115_cookie.structure_valid ? 'status-ok' : 'status-degraded'">{{ credentials.p115_cookie.structure_valid ? '结构正常' : '结构异常' }}</dd></div><div><dt>就绪状态</dt><dd :class="credentials.p115_cookie.ready ? 'status-ok' : 'status-degraded'">{{ credentials.p115_cookie.ready ? '已就绪' : '未就绪' }}</dd></div><div><dt>最后更新时间</dt><dd>{{ credentials.p115_cookie.last_updated_at ? formatTimestamp(credentials.p115_cookie.last_updated_at) : '未知' }}</dd></div></dl>
              <div class="credential-capabilities"><span>磁力云下载 <strong :class="p115?.capabilities.magnet ? 'status-ok' : 'status-degraded'">{{ p115 ? capabilityLabel(p115.capabilities.magnet) : '未知' }}</strong></span><span>115 分享转存 <strong :class="p115?.capabilities.share ? 'status-ok' : 'status-degraded'">{{ p115 ? (p115.capabilities.share ? '可用' : '未启用') : '未知' }}</strong></span></div>
              <div class="credential-form"><label>高级：手动 Cookie<input v-model="p115CookieDraft" type="password" autocomplete="new-password" spellcheck="false" aria-label="P115 Cookie" placeholder="扫码不可用时再输入" :disabled="credentialMutationBusy" /></label><div class="credential-actions"><button class="primary-button" type="button" :disabled="credentialMutationBusy || validationState === 'running'" @click="saveP115Credential"><LoaderCircle v-if="p115CredentialSaving" class="spin" :size="15" /><Save v-else :size="15" />保存并验证</button><button class="secondary-button" type="button" :disabled="credentialMutationBusy || validationState === 'running' || credentials.p115_cookie.source !== 'managed'" @click="resetP115Credential"><LoaderCircle v-if="p115CredentialResetting" class="spin" :size="15" /><RefreshCw v-else :size="15" />恢复文件 Cookie</button><button class="secondary-button" type="button" :disabled="p115MutationBusy || validationState === 'running'" @click="validateP115"><LoaderCircle v-if="validationState === 'running'" class="spin" :size="15" /><Cookie v-else :size="15" />验证当前 Cookie</button></div></div>
              <p v-if="p115CredentialError" class="settings-state settings-state-error credential-error" role="alert"><AlertTriangle :size="16" />{{ p115CredentialError }}<button v-if="p115CredentialError.includes('其他请求')" class="text-button" type="button" @click="loadCredentials">重新加载</button></p>
              <p v-if="validationMessage" :class="['settings-action-message', validationClass(validationState)]" role="status">{{ validationMessage }}</p>
              <div class="p115-qr-box">
                <div class="p115-qr-heading"><div><h4>扫码登录新设备</h4><p>每次扫码都会保存为独立设备，不会覆盖已保存的其他设备。</p></div><div class="p115-qr-options"><label class="settings-form-label">设备名称<input v-model="p115QrDeviceName" maxlength="64" :disabled="p115QrBusy" /></label><label class="settings-form-label">115 设备类型<select v-model="p115QrDeviceCode" aria-label="115 设备类型" :disabled="p115QrBusy"><option v-for="option in p115DeviceOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><button class="secondary-button" type="button" :disabled="p115QrBusy" @click="startP115QrLogin">{{ p115QrBusy ? '等待扫码' : '生成二维码' }}</button></div></div>
                <div v-if="p115QrImage" class="p115-qr-content"><img :src="p115QrImage" alt="115 登录二维码" /><div><p class="settings-note">请使用上方选择的设备类型扫码确认。</p><strong v-if="p115QrStatus === 'scanned'" class="status-ok">已扫码，等待确认</strong><strong v-else-if="p115QrStatus === 'waiting'" class="status-unknown">等待扫码</strong></div></div>
                <p v-if="p115QrError" class="settings-action-message" role="status">{{ p115QrError }}</p>
              </div>
              <div class="p115-device-list"><div class="p115-device-list-heading"><h4>已保存的登录设备</h4><button class="text-button" type="button" :disabled="p115DevicesLoading" @click="loadP115Devices"><LoaderCircle v-if="p115DevicesLoading" class="spin" :size="14" /><RefreshCw v-else :size="14" />{{ p115DevicesLoading ? '刷新中' : '刷新' }}</button></div><div v-if="p115DevicesLoading" class="settings-loading" role="status"><LoaderCircle class="spin" :size="18" />正在加载扫码设备</div><div v-else-if="p115DevicesError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ p115DevicesError }}</span><button class="text-button" type="button" @click="loadP115Devices">重试</button></div><p v-else-if="!p115Devices.length" class="settings-note">暂无扫码设备。手动 Cookie 不会显示在这里。</p><div v-else><div v-for="device in p115Devices" :key="device.id" class="p115-device-row"><div><strong>{{ device.name }}</strong><small>{{ device.device_code }} · {{ device.last_used_at ? formatTimestamp(device.last_used_at) : '未使用' }}</small></div><div><strong v-if="device.active" class="status-ok">当前使用</strong><button v-else class="text-button" type="button" @click="activateP115Device(device)">切换</button><button v-if="!device.active" class="text-button danger-text" type="button" @click="revokeP115Device(device)">移除</button></div></div></div></div>
              <p class="settings-note"><Cookie :size="15" />Cookie 仅使用服务端已配置的来源，页面不会回显已保存的 Cookie 原文。</p>
            </section>
          </div>
        </section>

        <section v-else-if="activeSection === 'prowlarr'" class="settings-section" aria-labelledby="prowlarr-title">
          <header class="settings-section-heading"><div><p class="eyebrow">多来源搜索</p><h2 id="prowlarr-title">Prowlarr</h2><p>配置由服务端保存，API Key 只提交到服务端，不会回显。</p></div><button class="icon-button" type="button" title="刷新 Prowlarr 状态" aria-label="刷新 Prowlarr 状态" :disabled="prowlarrLoading || prowlarrMutationBusy" @click="loadProwlarr"><RefreshCw :size="16" :class="{ spin: prowlarrLoading }" /></button></header>
          <div v-if="prowlarrLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载 Prowlarr 状态</div>
          <div v-else-if="prowlarrError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ prowlarrError }}</span><button class="text-button" type="button" @click="loadProwlarr">重试</button></div>
          <template v-else-if="prowlarr">
            <div class="p15-status-line prowlarr-status-line"><span class="settings-status-name"><Radio :size="17" />服务端配置</span><span :class="prowlarrStatusClass(prowlarr)">{{ prowlarrStatusLabel(prowlarr) }}</span></div>
            <p v-if="prowlarrHealthNote(prowlarr)" class="settings-note prowlarr-health-note" role="status">{{ prowlarrHealthNote(prowlarr) }}</p>
            <div class="settings-metrics prowlarr-metrics"><div class="settings-metric"><span>配置来源</span><strong>{{ prowlarrSourceLabel(prowlarr.source) }}</strong></div><div class="settings-metric"><span>API Key</span><strong :class="prowlarr.api_key_configured ? 'status-ok' : 'status-degraded'">{{ prowlarr.api_key_configured ? `已配置（${prowlarrApiKeySourceLabel(prowlarr.api_key_source)}）` : '未配置' }}</strong></div><div class="settings-metric"><span>配置版本</span><strong>{{ prowlarr.revision }}</strong></div><div class="settings-metric"><span>最后更新</span><strong>{{ prowlarr.last_updated_at ? formatTimestamp(prowlarr.last_updated_at) : '未知' }}</strong></div></div>
            <div class="settings-subsection"><h3>服务端配置</h3><div class="settings-form-grid"><label for="prowlarr-base-url">服务地址<input id="prowlarr-base-url" v-model="prowlarrBaseUrlDraft" type="url" inputmode="url" autocomplete="url" spellcheck="false" placeholder="例如 http://prowlarr:9696" :disabled="prowlarrMutationBusy" /></label><label for="prowlarr-api-key">API Key<input id="prowlarr-api-key" v-model="prowlarrApiKeyDraft" name="prowlarr_api_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="输入新的 API Key（可留空以保留现有配置）" :disabled="prowlarrMutationBusy" /></label></div><label class="settings-toggle"><input v-model="prowlarrEnabledDraft" type="checkbox" :disabled="prowlarrMutationBusy" />启用 Prowlarr 搜索来源</label><div class="settings-action-row"><button class="primary-button" type="button" :disabled="prowlarrMutationBusy || prowlarrValidationState === 'running'" @click="saveProwlarr"><LoaderCircle v-if="prowlarrSaving" class="spin" :size="15" /><Save v-else :size="15" />保存 Prowlarr 配置</button><button class="secondary-button" type="button" :disabled="prowlarrMutationBusy || prowlarrValidationState === 'running' || prowlarr.source !== 'managed'" @click="resetProwlarr"><LoaderCircle v-if="prowlarrResetting" class="spin" :size="15" /><RefreshCw v-else :size="15" />恢复环境配置</button></div><p class="settings-note"><ShieldCheck :size="15" />API Key 仅在填写后随保存请求提交；服务端响应、状态和日志不会包含 Key 原文。</p></div>
            <p v-if="prowlarrSaveMessage" class="settings-action-message status-ok" role="status">{{ prowlarrSaveMessage }}</p><p v-if="prowlarrSaveError" class="settings-state settings-state-error settings-save-error" role="alert"><AlertTriangle :size="17" />{{ prowlarrSaveError }}<button v-if="prowlarrConflict" class="text-button" type="button" @click="loadProwlarr">重新加载</button></p>
            <div class="settings-subsection"><h3>只读连接验证</h3><div class="settings-action-row"><button class="secondary-button" type="button" :disabled="prowlarrValidationState === 'running' || prowlarrMutationBusy" @click="validateProwlarr"><LoaderCircle v-if="prowlarrValidationState === 'running'" class="spin" :size="16" /><Radio v-else :size="16" />验证 Prowlarr 连接</button><span v-if="prowlarrValidationMessage" :class="['settings-action-message', prowlarrValidationClass(prowlarrValidationState)]" role="status">{{ prowlarrValidationMessage }}</span></div><p class="settings-note">验证只读使用服务端已保存的配置，不会修改 URL、启用状态或 API Key。</p></div>
          </template>
        </section>

        <section v-else-if="activeSection === 'organization'" class="settings-section" aria-labelledby="organization-title">
          <header class="settings-section-heading"><div><p class="eyebrow">115 网盘</p><h2 id="organization-title">自动整理</h2><p>定时开关只控制自动触发；手动运行请在整理页发起。</p></div><div class="settings-section-actions"><button class="secondary-button" type="button" @click="openOrganizationPage"><Settings2 :size="15" />前往整理页</button></div></header>
          <div v-if="organizationLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载整理设置</div>
          <div v-else-if="organizationError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ organizationError }}</span><button class="text-button" type="button" @click="loadOrganization">重试</button></div>
          <template v-else-if="organizationSettings">
            <details class="settings-subsection" open>
              <summary><h3>整理执行</h3><span class="settings-section-disclosure" aria-hidden="true">⌄</span></summary>
              <label class="settings-toggle"><input v-model="organizationDraft.schedule_enabled" type="checkbox" />定时整理：启用后按扫描间隔自动整理；关闭后不自动整理，可到整理页手动触发</label>
              <label class="settings-toggle"><input v-model="organizationDraft.auto_execute_enabled" type="checkbox" />自动整理：识别高置信度的影片自动确认并排队，识别不确定的保留在来源目录，可在“整理”页待处理列表中人工处理</label>
              <div class="settings-form-grid"><label>扫描频率（分钟）<input v-model.number="organizationDraft.scan_interval_minutes" type="number" min="5" max="1440" /></label></div>
              <p class="settings-note">当前状态：{{ organizationDraft.schedule_enabled ? '定时整理已启用' : '定时整理已关闭' }}。停止定时会自动关闭开关并保存，正在执行的远端操作不会被强行中断。</p>
            </details>
             <details class="settings-subsection" open><summary><h3>扫描来源、归档与推送目录</h3><span class="settings-section-disclosure" aria-hidden="true">⌄</span></summary><p class="settings-note">目录选择器从 115 网盘根目录开始浏览。扫描来源可多选，整理归档目录和资源推送目录各选一个；三者保存后分别生效。页面主显示使用可复核的目录名称/相对路径，CID 仅在折叠诊断信息中脱敏显示。</p><div class="directory-selection-grid"><div class="directory-selection-field"><span>整理扫描来源</span><div class="directory-chips"><span v-for="(id, index) in organizationList(organizationSourceDraft)" :key="id" class="directory-chip"><span>{{ sourceDirectoryDisplayLabel(index) }}</span><button type="button" aria-label="移除扫描来源" @click="removeOrganizationSource(index)">×</button></span><span v-if="!organizationList(organizationSourceDraft).length" class="settings-note">尚未选择</span></div><button class="secondary-button" type="button" @click="openDirectoryPicker('source')">📂 选择扫描来源</button></div><div class="directory-selection-field"><span>整理归档目录</span><span v-if="organizationTargetDraft" class="directory-chip"><span>{{ organizationTargetLabelDraft || '目录名称待确认' }}</span></span><span v-else class="settings-note">尚未选择</span><button class="secondary-button" type="button" @click="openDirectoryPicker('target')">📂 选择归档目录</button></div><div class="directory-selection-field"><span>资源推送目录</span><span v-if="organizationPushDraft" class="directory-chip"><span>{{ organizationPushLabelDraft || '目录名称待确认' }}</span></span><span v-else class="settings-note">尚未选择</span><button class="secondary-button" type="button" @click="openDirectoryPicker('push')">📂 选择推送目录</button><p class="settings-note">资源页面点击“推送”时直接使用这里保存的目录，不再临时选择。</p></div></div><details class="directory-diagnostic"><summary>诊断信息</summary><small>扫描来源 CID（脱敏）：{{ redactedDirectoryIds(organizationSourceDraft) }}</small><small>归档目录 CID（脱敏）：{{ redactDirectoryId(organizationTargetDraft) }}</small><small>推送目录 CID（脱敏）：{{ redactDirectoryId(organizationPushDraft) }}</small></details></details>
            <details class="settings-subsection"><summary><h3>高级设置</h3><span class="settings-section-disclosure" aria-hidden="true">⌄</span></summary>
            <div class="settings-subsection"><h3>识别与命名</h3><div class="settings-form-grid"><label>视频文件类型（逗号分隔）<input v-model="organizationVideoExtensionsDraft" placeholder="mkv, mp4, avi" /></label><label>字幕/元数据类型（逗号分隔）<input v-model="organizationMetadataExtensionsDraft" placeholder="srt, ass, nfo" /></label></div><div class="settings-capability-list"><label class="settings-toggle"><input v-model="organizationDraft.rename_enabled" type="checkbox" />标准化重命名</label><label class="settings-toggle"><input v-model="organizationDraft.media_probe_enabled" type="checkbox" />媒体信息提取完善命名</label><label class="settings-toggle"><input v-model="organizationDraft.ai_identification_enabled" type="checkbox" />AI 辅助识别</label></div><p class="settings-note">AI 辅助识别需要先在实用工具完成 API 配置；未配置时不会调用 AI。</p></div>
            <div class="settings-subsection"><h3>整理规则</h3><div class="settings-form-grid"><label>小文件过滤（MB）<input v-model.number="organizationDraft.small_file_threshold_mb" type="number" min="0" step="0.1" /></label><label>操作延时（秒）<input v-model.number="organizationDraft.operation_delay_seconds" type="number" min="0" max="60" step="0.1" /></label></div><div class="settings-capability-list"><label class="settings-toggle"><input v-model="organizationDraft.cleanup_empty_directories" type="checkbox" />整理后清理空文件夹</label><label class="settings-toggle"><input v-model="organizationDraft.strm_linkage_enabled" type="checkbox" />联动生成 STRM</label></div></div>
            <div class="settings-subsection"><h3>分类策略</h3><div class="settings-capability-list"><label class="settings-toggle"><input v-model="organizationDraft.include_children_category" type="checkbox" />添加儿童节目分类</label><label class="settings-toggle"><input v-model="organizationDraft.include_concert_category" type="checkbox" />添加演唱会分类</label><label class="settings-toggle"><input v-model="organizationDraft.region_grouping_enabled" type="checkbox" />按地区二次分类</label><label class="settings-toggle"><input v-model="organizationDraft.year_grouping_enabled" type="checkbox" />按年份三次分类</label></div></div>
            <div class="settings-subsection"><h3>覆盖策略</h3><div class="settings-capability-list"><label class="settings-toggle"><input v-model="organizationDraft.prefer_remux" type="checkbox" />Remux/蓝光优先</label><label class="settings-toggle"><input v-model="organizationDraft.prefer_resolution" type="checkbox" />大分辨率优先</label><label class="settings-toggle"><input v-model="organizationDraft.prefer_dolby" type="checkbox" />杜比优先</label><label>冲突处理方式<select v-model.number="organizationDraft.conflict_mode"><option :value="2">不主动覆盖（仅检查同名）</option><option :value="1">进行覆盖：大文件优先</option><option :value="0">进行覆盖：小文件优先</option></select></label><label class="settings-toggle"><input v-model="organizationDraft.multi_version_enabled" type="checkbox" />保留杜比 + 非杜比多版本</label></div><p class="settings-note">真实移动、重命名和覆盖仍需经过已确认计划、远端前置条件核对、幂等和审计门禁。</p></div>
            </details>
            <div v-if="organizationDirty" class="settings-save-bar"><span>有未保存的整理设置</span><div><button class="secondary-button" type="button" :disabled="organizationSaving" @click="loadOrganization">取消</button><button class="primary-button" type="button" :disabled="organizationSaving" @click="saveOrganization"><LoaderCircle v-if="organizationSaving" class="spin" :size="15" /><Save v-else :size="15" />保存</button></div></div>
            <div v-if="organizationSaveError" class="settings-state settings-state-error settings-save-error" role="alert"><AlertTriangle :size="17" /><span>{{ organizationSaveError }}</span><button class="text-button" type="button" @click="loadOrganization">重新加载</button></div>
          </template>
        </section>

        <div v-if="directoryPickerOpen" class="directory-picker-backdrop" role="presentation" @click.self="directoryPickerOpen = false">
          <section class="directory-picker" role="dialog" aria-modal="true" aria-labelledby="directory-picker-title">
             <header class="directory-picker-heading"><div><p class="eyebrow">115 网盘</p><h2 id="directory-picker-title">选择{{ directoryPickerMode === 'source' ? '扫描来源' : directoryPickerMode === 'target' ? '归档目标' : '资源推送' }}目录</h2><p>相对路径：{{ directoryPickerCurrentPath || '根目录' }}</p><details class="directory-diagnostic"><summary>诊断信息</summary><small>CID（脱敏）：{{ redactDirectoryId(directoryPickerCurrentId) }}</small></details></div><button class="icon-button" type="button" aria-label="关闭目录选择器" title="关闭" @click="directoryPickerOpen = false">×</button></header>
            <div v-if="directoryPickerLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在读取目录</div>
            <div v-else-if="directoryPickerError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ directoryPickerError }}</span><button class="text-button" type="button" @click="loadDirectoryPicker">重试</button></div>
           <template v-else><div class="directory-picker-toolbar"><button class="secondary-button" type="button" :disabled="!directoryPickerTrail.length" @click="leaveDirectory">返回上级</button><button class="primary-button" type="button" :disabled="!directoryPickerCurrentId || directoryPickerCurrentId === '0'" @click="chooseDirectory">{{ directoryPickerCurrentId === '0' ? '根目录不可直接选择' : '选择当前目录' }}</button></div><div v-if="!directoryPickerItems.length" class="settings-empty-block">当前目录没有可浏览的子目录。</div><div class="directory-picker-list"><button v-for="item in directoryPickerItems" :key="item.id" type="button" class="directory-picker-item" @click="enterDirectory(item)"><span>📁</span><span>{{ item.name }}</span><span>进入</span></button></div></template>
          </section>
        </div>

        <section v-else-if="activeSection === 'logs'" class="settings-section" aria-labelledby="logs-title">
          <header class="settings-section-heading"><div><p class="eyebrow">事件流</p><h2 id="logs-title">日志</h2><p>日志事件流已独立展示，这里只配置保留策略。</p></div><button class="secondary-button" type="button" @click="openLogsPage"><FileText :size="15" />打开日志查看器</button></header>
          <div class="settings-subsection logging-settings"><h3>日志保留</h3><div v-if="loggingLoading" class="settings-loading"><LoaderCircle class="spin" :size="18" />正在加载日志设置</div><div v-else-if="loggingError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ loggingError }}</span><button class="text-button" type="button" @click="loadLogging">重试</button></div><template v-else-if="logging"><div class="settings-form-grid"><label>最低级别<select v-model="draftLevel"><option v-for="option in logLevelOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><label>保留天数（1-90）<input v-model.number="draftRetentionDays" type="number" min="1" max="90" /></label><label>文件上限（MB，1-50）<input v-model.number="draftMaxFileMb" type="number" min="1" max="50" /></label></div><div v-if="loggingDirty" class="settings-save-bar"><span>有未保存的日志设置</span><div><button class="secondary-button" type="button" :disabled="savingLogging" @click="loadLogging">取消</button><button class="primary-button" type="button" :disabled="savingLogging" @click="saveLogging"><LoaderCircle v-if="savingLogging" class="spin" :size="15" /><Save v-else :size="15" />保存</button></div></div><div v-if="saveError" class="settings-state settings-state-error settings-save-error" role="alert"><AlertTriangle :size="17" /><span>{{ saveError }}</span><button v-if="conflict" class="text-button" type="button" @click="loadLogging">重新加载</button></div></template></div>
        </section>

        <section v-else-if="activeSection === 'content'" class="settings-section" aria-labelledby="content-policy-title">
          <header class="settings-section-heading"><div><p class="eyebrow">内容安全</p><h2 id="content-policy-title">内容安全</h2></div><button class="icon-button" type="button" title="刷新内容安全设置" aria-label="刷新内容安全设置" :disabled="contentLoading" @click="loadContentPolicy"><RefreshCw :size="16" :class="{ spin: contentLoading }" /></button></header>
          <div v-if="contentLoading && !contentPolicy" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载内容安全设置</div>
          <div v-else-if="contentError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ contentError }}</span><button class="text-button" type="button" @click="loadContentPolicy">重试</button></div>
          <template v-else-if="contentPolicy">
            <div class="settings-policy-list">
              <label class="settings-policy-row"><span><strong>过滤成人媒体</strong><small>不在首页、目录和搜索结果中显示 TMDB 标记内容</small></span><input v-model="contentDraftHideAdultMedia" type="checkbox" /></label>
              <label class="settings-policy-row"><span><strong>过滤可疑资源</strong><small>屏蔽高置信成人来源和明确标记</small></span><input v-model="contentDraftHideSuspiciousResources" type="checkbox" /></label>
              <label class="settings-policy-row"><span><strong>过滤低质量资源</strong><small>屏蔽 CAM、枪版等高置信低质量标记</small></span><input v-model="contentDraftHideLowQualityResources" type="checkbox" /></label>
            </div>
            <label class="content-keywords"><span>自定义屏蔽词</span><textarea v-model="blockedKeywordsDraft" rows="5" maxlength="2048" placeholder="每行一个关键词" /></label>
            <div class="settings-save-bar"><span>当前版本 {{ contentPolicy.revision }}</span><button class="primary-button" type="button" :disabled="contentSaving" @click="saveContentPolicy"><LoaderCircle v-if="contentSaving" class="spin" :size="15" /><Save v-else :size="15" />保存</button></div>
            <div v-if="contentSaveError" class="settings-state settings-state-error settings-save-error" role="alert"><AlertTriangle :size="17" /><span>{{ contentSaveError }}</span><button v-if="contentConflict" class="text-button" type="button" @click="loadContentPolicy">重新加载</button></div>
          </template>
        </section>

        <section v-else-if="activeSection === 'inspection'" class="settings-section" aria-labelledby="inspection-title">
          <header class="settings-section-heading"><div><p class="eyebrow">资源检测</p><h2 id="inspection-title">资源检测</h2></div><button class="icon-button" type="button" title="刷新资源检测设置" aria-label="刷新资源检测设置" :disabled="inspectionLoading" @click="loadInspection"><RefreshCw :size="16" :class="{ spin: inspectionLoading }" /></button></header>
          <div v-if="inspectionLoading" class="settings-loading"><LoaderCircle class="spin" :size="20" />正在加载资源检测设置</div>
          <div v-else-if="inspectionError" class="settings-state settings-state-error" role="alert"><AlertTriangle :size="18" /><span>{{ inspectionError }}</span><button class="text-button" type="button" @click="loadInspection">重试</button></div>
          <template v-else-if="inspectionSettings">
            <div class="settings-subsection"><h3>打开详情时自动检测</h3><label class="settings-toggle"><input v-model="inspectionDraft" type="checkbox" aria-label="打开详情时自动检测" /><span>自动提交首批资源检测</span></label><p class="settings-note">关闭后仍可在资源详情中手动开始检测。</p></div>
            <div v-if="inspectionDirty" class="settings-save-bar"><span>有未保存的资源检测设置</span><div><button class="secondary-button" type="button" :disabled="savingInspection" @click="loadInspection">取消</button><button class="primary-button" type="button" :disabled="savingInspection" @click="saveInspection"><LoaderCircle v-if="savingInspection" class="spin" :size="15" /><Save v-else :size="15" />保存</button></div></div>
            <div v-if="inspectionSaveError" class="settings-state settings-state-error settings-save-error" role="alert"><AlertTriangle :size="17" /><span>{{ inspectionSaveError }}</span><button v-if="inspectionConflict" class="text-button" type="button" @click="loadInspection">重新加载</button></div>
          </template>
        </section>

      </div>
    </div>
  </section>
</template>
