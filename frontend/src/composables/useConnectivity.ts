import { ref } from "vue";
import { browserIsOnline } from "../api";

/** 在线状态与离线缓存时间戳。 */
export function useConnectivity() {
  const isOnline = ref(browserIsOnline());
  const offlineDataAt = ref<string | null>(null);

  function updateOnline(): void {
    isOnline.value = browserIsOnline();
  }

  function recordOfflineData(event: Event): void {
    const cachedAt = (event as CustomEvent<{ cachedAt?: string }>).detail?.cachedAt;
    if (cachedAt) offlineDataAt.value = cachedAt;
  }

  return { isOnline, offlineDataAt, updateOnline, recordOfflineData };
}
