/** Generic polling helper shared by long-running operation views. */

export interface PollUntilOptions<T> {
  /** Interval between fetches in milliseconds. */
  intervalMs?: number;
  /** Maximum number of attempts before giving up. */
  maxAttempts?: number;
  /** Returns false when the caller's request/session is stale and polling must stop. */
  isCurrent?: () => boolean;
  /** Invoked with each successful response. */
  onResponse?: (value: T) => void;
  /** Returns true when the poll is done (terminal state). */
  isDone?: (value: T) => boolean;
}

/**
 * Poll a fetch callback until a terminal state, a stale guard, or an attempt
 * cap. Each iteration waits one interval before fetching, matching the
 * delayed-first-tick behavior of the setInterval/setTimeout loops it replaces.
 * Returns the last successful value, or null when the poll was stopped early
 * (stale/aborted/timeout) without a terminal response.
 */
export async function pollUntil<T>(
  fetchValue: () => Promise<T>,
  options: PollUntilOptions<T> = {},
): Promise<T | null> {
  const intervalMs = options.intervalMs ?? 1000;
  const maxAttempts = options.maxAttempts ?? 60;
  const isCurrent = options.isCurrent ?? (() => true);
  // L13: 默认 isDone 必须保守(永不立即完成)。旧默认 (() => true) 会让
  // 遗漏 isDone 的调用方静默单次抓取即返回,把"轮询直到终态"变成"取一次
  // 初始状态"。改为 () => false 后,遗漏者完整轮询到 maxAttempts 再超时返回。
  const isDone = options.isDone ?? (() => false);
  let current: T;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (!isCurrent()) return null;
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
    if (!isCurrent()) return null;
    try {
      current = await fetchValue();
    } catch {
      // The caller's fetch callback already surfaced the error in its own
      // catch block; stop polling without letting the rejection escape as an
      // unhandled promise rejection (which Vitest treats as a fatal error).
      return null;
    }
    if (!isCurrent()) return null;
    options.onResponse?.(current);
    if (isDone(current)) return current;
  }
  return null;
}
