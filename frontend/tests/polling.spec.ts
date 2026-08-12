import { afterEach, describe, expect, it, vi } from "vitest";

import { pollUntil } from "../src/polling";

afterEach(() => {
  vi.useRealTimers();
});

describe("pollUntil default isDone (L13)", () => {
  it("does not complete on the first fetch when isDone is omitted", async () => {
    // L13 陷阱修复:旧默认 isDone=()=>true 会让遗漏 isDone 的调用方
    // 静默单次抓取即返回。新默认 () => false 必须完整轮询到 maxAttempts。
    vi.useFakeTimers();
    let fetches = 0;
    const promise = pollUntil(
      async () => {
        fetches += 1;
        return { status: "running" as const };
      },
      { intervalMs: 10, maxAttempts: 3 },
    );
    await vi.advanceTimersByTimeAsync(500);
    const result = await promise;
    expect(fetches).toBeGreaterThan(1);
    expect(result).toBeNull();
  });

  it("returns the terminal value when isDone is provided", async () => {
    vi.useFakeTimers();
    const promise = pollUntil(
      async () => ({ status: "organized" as const }),
      {
        intervalMs: 10,
        maxAttempts: 5,
        isDone: (current) => current.status === "organized",
      },
    );
    await vi.advanceTimersByTimeAsync(500);
    const result = await promise;
    expect(result).toEqual({ status: "organized" });
  });
});
