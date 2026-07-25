import type { HealthResponse, ResourceSummary } from "./types";

export interface PushCapabilities {
  magnet: boolean;
  share: boolean;
}

export const NO_PUSH_CAPABILITIES: PushCapabilities = { magnet: false, share: false };

export function resolvePushCapabilities(health: HealthResponse): PushCapabilities {
  const capabilities = health.push_capabilities;
  if (capabilities !== undefined) {
    return typeof capabilities === "object"
      && capabilities !== null
      && typeof capabilities.magnet === "boolean"
      && typeof capabilities.share === "boolean"
      ? { magnet: capabilities.magnet, share: capabilities.share }
      : { ...NO_PUSH_CAPABILITIES };
  }
  const enabled = health.push_supported === true;
  return { magnet: enabled, share: enabled };
}

export function canPushResource(
  resource: Pick<ResourceSummary, "kind">,
  capabilities: PushCapabilities,
): boolean {
  if (resource.kind === "magnet") return capabilities.magnet;
  if (resource.kind === "115_share") return capabilities.share;
  return false;
}

export async function submitPushResource<T>(
  resource: Pick<ResourceSummary, "resource_id" | "kind">,
  capabilities: PushCapabilities,
  submit: (resourceId: string) => Promise<T>,
): Promise<T | null> {
  if (!canPushResource(resource, capabilities)) return null;
  return submit(resource.resource_id);
}
