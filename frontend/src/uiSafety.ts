const SAFE_MACHINE_CODE = /^[a-z][a-z0-9_.-]{1,99}$/;

export function diagnosticCode(value: unknown): string {
  if (typeof value !== "string") return "unknown_error";
  const candidate = value.trim();
  return SAFE_MACHINE_CODE.test(candidate) ? candidate : "unknown_error";
}

export function diagnosticReference(value: unknown): string {
  if (typeof value !== "string") return "未提供";
  const candidate = value.trim();
  if (!candidate) return "未提供";
  if (candidate.length <= 14) return candidate;
  return `${candidate.slice(0, 8)}...${candidate.slice(-4)}`;
}

export function safeLocalizedCopy(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const candidate = value.trim();
  if (!candidate || candidate.length > 400) return fallback;
  if (/(?:magnet:\?xt=|(?:cookie|token|pickcode|password|secret)\s*[:=]\s*\S+)/i.test(candidate)) return fallback;
  return candidate;
}
