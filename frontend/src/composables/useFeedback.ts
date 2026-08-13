import { ref, type Ref } from "vue";

export type ToastLevel = "success" | "info" | "warning" | "error";

export interface ToastItem {
  id: number;
  level: ToastLevel;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
  /** ms; undefined => 按级别默认（error 常驻） */
  duration?: number;
}

export interface ToastOptions {
  actionLabel?: string;
  onAction?: () => void;
  duration?: number;
}

const DEFAULT_DURATION: Record<ToastLevel, number> = {
  success: 3500,
  info: 3500,
  warning: 5000,
  error: Number.POSITIVE_INFINITY,
};

const MAX_TOASTS = 4;
const DEDUP_WINDOW_MS = 2000;

const toasts = ref<ToastItem[]>([]);
const recent: Array<{ key: string; at: number }> = [];
let nextId = 1;

function dedupKey(level: ToastLevel, message: string): string {
  return `${level}:${message}`;
}

function pruneRecent(now: number): void {
  while (recent.length && now - recent[0].at > DEDUP_WINDOW_MS) recent.shift();
}

export function useFeedback(): {
  toasts: Readonly<Ref<ToastItem[]>>;
  success: (message: string, opts?: ToastOptions) => void;
  info: (message: string, opts?: ToastOptions) => void;
  warning: (message: string, opts?: ToastOptions) => void;
  error: (message: string, opts?: ToastOptions) => void;
  dismiss: (id: number) => void;
  clear: () => void;
} {
  function push(level: ToastLevel, message: string, opts?: ToastOptions): void {
    const now = Date.now();
    pruneRecent(now);
    const key = dedupKey(level, message);
    if (recent.some((entry) => entry.key === key)) return;
    recent.push({ key, at: now });
    const item: ToastItem = {
      id: nextId,
      level,
      message,
      ...(opts?.actionLabel ? { actionLabel: opts.actionLabel } : {}),
      ...(opts?.onAction ? { onAction: opts.onAction } : {}),
      ...(opts?.duration !== undefined ? { duration: opts.duration } : {}),
    };
    nextId += 1;
    toasts.value = [...toasts.value, item];
    if (toasts.value.length > MAX_TOASTS) {
      toasts.value = toasts.value.slice(toasts.value.length - MAX_TOASTS);
    }
  }

  function dismiss(id: number): void {
    toasts.value = toasts.value.filter((item) => item.id !== id);
  }

  function clear(): void {
    toasts.value = [];
    recent.length = 0;
  }

  return {
    toasts,
    success: (message, opts) => push("success", message, opts),
    info: (message, opts) => push("info", message, opts),
    warning: (message, opts) => push("warning", message, opts),
    error: (message, opts) => push("error", message, opts),
    dismiss,
    clear,
  };
}

export { DEFAULT_DURATION };
