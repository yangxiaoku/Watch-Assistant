import { ref } from "vue";
import { NO_PUSH_CAPABILITIES, resolvePushCapabilities, type PushCapabilities } from "../push";
import type { CapabilityAvailability, HealthResponse } from "../types";

/** 从健康接口解析出的能力开关状态集合。 */
export function useCapabilities() {
  const unavailableCapability = (settingsSection: CapabilityAvailability["settings_section"] = "overview"): CapabilityAvailability => ({
    enabled: false,
    reason_code: "capability_unknown",
    reason_zh: "当前未读取能力状态，请刷新页面后重试。",
    settings_section: settingsSection,
  });

  const pushCapabilities = ref<PushCapabilities>({ ...NO_PUSH_CAPABILITIES });
  const inspectionSupported = ref(false);
  const inspectionAutoStartEnabled = ref<boolean | "unknown">("unknown");
  const organizationPlanCapability = ref<CapabilityAvailability>(unavailableCapability());
  const organizationPlanEnabled = ref(false);
  const organizationExecutionSupported = ref(false);
  const strmFullCapability = ref<CapabilityAvailability>(unavailableCapability());
  const strmIncrementalCapability = ref<CapabilityAvailability>(unavailableCapability());
  const strmCleanupCapability = ref<CapabilityAvailability>(unavailableCapability());
  const emptyDirectoryCleanupCapability = ref<CapabilityAvailability>(unavailableCapability());

  function healthCapability(
    enabled: boolean | undefined,
    reasonCode: string,
    label: string,
    settingsSection: CapabilityAvailability["settings_section"] = "overview",
  ): CapabilityAvailability {
    if (enabled === undefined) return unavailableCapability(settingsSection);
    return {
      enabled,
      reason_code: enabled ? null : reasonCode,
      reason_zh: enabled ? "可执行" : `${label}未启用，请在${settingsSection === "organization" ? "自动整理设置" : "设置概览"}查看功能状态。`,
      settings_section: settingsSection,
    };
  }

  function applyHealth(health: HealthResponse) {
    pushCapabilities.value = resolvePushCapabilities(health);
    inspectionSupported.value = health.inspection_supported === true;
    inspectionAutoStartEnabled.value = health.inspection_auto_start_enabled === true;
    organizationPlanCapability.value = healthCapability(health.organization_plan_enabled, "organization_plan_disabled", "自动整理计划", "organization");
    organizationPlanEnabled.value = organizationPlanCapability.value.enabled;
    organizationExecutionSupported.value = health.organization_execution_supported === true;
    strmFullCapability.value = healthCapability(health.strm_capabilities?.full, "strm_full_disabled", "STRM 全量生成");
    strmIncrementalCapability.value = healthCapability(health.strm_capabilities?.incremental, "strm_incremental_disabled", "STRM 增量同步");
    const strmCleanup = health.strm_capabilities?.cleanup_capability;
    strmCleanupCapability.value = strmCleanup ?? {
      enabled: health.strm_capabilities?.cleanup === true,
      reason_code: health.strm_capabilities?.cleanup === true ? null : "strm_cleanup_disabled",
      reason_zh: health.strm_capabilities?.cleanup === true
        ? "可用"
        : "STRM 失效清理未启用，请检查部署功能开关。",
      settings_section: "overview",
    };
    emptyDirectoryCleanupCapability.value = health.organization_capabilities?.empty_directory_cleanup
      ?? unavailableCapability();
  }

  return {
    pushCapabilities,
    inspectionSupported,
    inspectionAutoStartEnabled,
    organizationPlanCapability,
    organizationPlanEnabled,
    organizationExecutionSupported,
    strmFullCapability,
    strmIncrementalCapability,
    strmCleanupCapability,
    emptyDirectoryCleanupCapability,
    healthCapability,
    applyHealth,
  };
}
