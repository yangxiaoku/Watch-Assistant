import type { ResourceSummary } from "./types";

const sourceLabels: Record<string, string> = {
  pansou: "PanSou",
  "plugin:pansou": "PanSou",
  prowlarr: "Prowlarr",
  "plugin:prowlarr": "Prowlarr",
};

function cleanNames(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function sourceLabel(value: string): string {
  const trimmed = value.trim();
  return sourceLabels[trimmed.toLowerCase()] ?? (trimmed || "未知来源");
}

export function resourceSourceNames(resource: ResourceSummary): string[] {
  return [sourceLabel(resource.source)];
}

export function resourceSourceLabel(resource: ResourceSummary): string {
  return resourceSourceNames(resource)[0];
}

export function sourceNameList(values: string[]): string[] {
  return cleanNames(values.map(sourceLabel));
}
