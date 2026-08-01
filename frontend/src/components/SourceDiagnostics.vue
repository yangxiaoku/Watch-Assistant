<script setup lang="ts">
import { AlertTriangle } from "@lucide/vue";
import { computed } from "vue";

const props = defineProps<{ warnings?: string[] }>();

const sourceWarnings = computed(() => (props.warnings ?? []).filter((warning) => {
  return warning === "partial_upstream"
    || warning === "prowlarr_unsupported_results"
    || warning === "prowlarr_results_truncated"
    || warning.startsWith("prowlarr_query_failed:")
    || warning.startsWith("pansou_query_failed:");
}));

const hasQueryFailure = computed(() => sourceWarnings.value.some((warning) => warning.includes("_query_failed:")));

function warningLabel(warning: string): string {
  if (warning === "partial_upstream") return "部分搜索来源未完成本次查询";
  if (warning === "prowlarr_unsupported_results") return "Prowlarr 返回了暂不支持的资源类型";
  if (warning === "prowlarr_results_truncated") return "Prowlarr 连续满页达到上限，结果可能不完整";
  if (warning.startsWith("prowlarr_query_failed:")) return "Prowlarr 查询失败";
  if (warning.startsWith("pansou_query_failed:")) return "PanSou 查询失败";
  return "部分搜索来源出现异常";
}

const messages = computed(() => {
  const specificWarnings = sourceWarnings.value.filter((warning) => warning !== "partial_upstream");
  return [...new Set((specificWarnings.length ? specificWarnings : sourceWarnings.value).map(warningLabel))];
});
</script>

<template>
  <aside v-if="sourceWarnings.length" class="source-diagnostics" role="status" aria-live="polite">
    <AlertTriangle :size="17" />
    <div class="source-diagnostics-copy">
      <strong>{{ hasQueryFailure ? "部分搜索来源暂不可用" : "搜索来源提示" }}</strong>
      <p>{{ messages.join("；") }}。已保留其他来源的有效结果。</p>
    </div>
  </aside>
</template>
