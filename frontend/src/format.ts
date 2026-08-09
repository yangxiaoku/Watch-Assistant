/** Shared formatting helpers (bytes, timestamps, poster URLs). */

export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString("zh-CN", { hour12: false });
}

export function posterUrl(
  path: string | null | undefined,
  size: "w342" | "w500" | "w1280" = "w500",
): string | null {
  return path ? `https://image.tmdb.org/t/p/${size}${path}` : null;
}
