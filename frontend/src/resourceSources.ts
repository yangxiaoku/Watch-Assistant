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
  const values = resource.sources?.length ? resource.sources : [resource.source];
  return cleanNames(values.map(sourceLabel));
}

export function resourceSourceLabel(resource: ResourceSummary): string {
  return resourceSourceNames(resource)[0] ?? sourceLabel(resource.source);
}

export function resourceSourceCount(resource: ResourceSummary): number {
  const names = resourceSourceNames(resource);
  return resource.source_count && resource.source_count > 0 ? resource.source_count : names.length;
}

export function sourceNameList(values: string[]): string[] {
  return cleanNames(values.map(sourceLabel));
}
