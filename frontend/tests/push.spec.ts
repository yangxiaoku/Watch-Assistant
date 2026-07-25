import { describe, expect, it, vi } from "vitest";

import { canPushResource, NO_PUSH_CAPABILITIES, resolvePushCapabilities, submitPushResource } from "../src/push";
import type { HealthResponse, ResourceSummary } from "../src/types";

const magnet: ResourceSummary = {
  resource_id: "magnet-1",
  kind: "magnet",
  name: "Magnet",
  size_bytes: null,
  seeders: null,
  source: "test",
  captured_at: "2026-07-24T10:00:00Z",
};
const share: ResourceSummary = { ...magnet, resource_id: "share-1", kind: "115_share", name: "115 分享" };

describe("push capabilities", () => {
  it("prefers valid new capabilities over the legacy aggregate flag", () => {
    expect(resolvePushCapabilities({
      status: "ok",
      push_supported: false,
      push_capabilities: { magnet: true, share: false },
    })).toEqual({ magnet: true, share: false });
  });

  it("falls back to the legacy aggregate flag", () => {
    const enabled = resolvePushCapabilities({ status: "ok", push_supported: true });
    const disabled = resolvePushCapabilities({ status: "ok", push_supported: false });
    expect(enabled).toEqual({ magnet: true, share: true });
    expect(canPushResource(magnet, enabled)).toBe(true);
    expect(canPushResource(share, enabled)).toBe(true);
    expect(disabled).toEqual(NO_PUSH_CAPABILITIES);
    expect(canPushResource(magnet, disabled)).toBe(false);
    expect(canPushResource(share, disabled)).toBe(false);
  });

  it("closes both capabilities for an invalid capability object", () => {
    expect(resolvePushCapabilities({
      status: "ok",
      push_supported: true,
      push_capabilities: { magnet: true } as HealthResponse["push_capabilities"],
    })).toEqual(NO_PUSH_CAPABILITIES);
  });

  it("gates resources by kind and submits an enabled magnet once", async () => {
    const capabilities = { magnet: true, share: false };
    const createTask = vi.fn().mockResolvedValue({ id: "task-1" });

    expect(canPushResource(magnet, capabilities)).toBe(true);
    expect(canPushResource(share, capabilities)).toBe(false);
    expect(await submitPushResource(magnet, capabilities, createTask)).toEqual({ id: "task-1" });
    expect(await submitPushResource(share, capabilities, createTask)).toBeNull();
    expect(createTask).toHaveBeenCalledTimes(1);
    expect(createTask).toHaveBeenCalledWith("magnet-1");
  });

  it("fails closed for an unknown resource kind", () => {
    expect(canPushResource({ kind: "unknown" } as Pick<ResourceSummary, "kind">, { magnet: true, share: true })).toBe(false);
  });
});
