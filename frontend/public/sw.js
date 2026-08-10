const CACHE_PREFIX = "watch-assistant-";
const CACHE_NAME = `${CACHE_PREFIX}shell-v2`;
const READ_CACHE_NAME = `${CACHE_PREFIX}read-v2`;
const APP_SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg"];
const READ_ONLY_API = /^\/api\/v1\/(health|tasks(?:[/?]|$)|notifications(?:[/?]|$)|workflows(?:[/?]|$))/;
const INTERNAL_DEEP_LINK = /^\/(?:tasks|notifications|workflows|settings|organization-plans)(?:[/?#]|$)/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && ![CACHE_NAME, READ_CACHE_NAME].includes(key))
          .map((key) => caches.delete(key)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (READ_ONLY_API.test(url.pathname)) {
    event.respondWith(
      fetch(request).then((response) => {
        if (
          !response.ok ||
          response.type === "opaque" ||
          response.headers.has("Set-Cookie") ||
          response.headers.get("Cache-Control")?.toLowerCase().includes("no-store") ||
          !response.headers.get("Content-Type")?.toLowerCase().includes("application/json")
        ) return response;
        const headers = new Headers(response.headers);
        headers.set("X-WA-Cached-At", new Date().toISOString());
        const cacheResponse = response.clone();
        return caches.open(READ_CACHE_NAME).then((cache) => {
          void cache.put(request, new Response(cacheResponse.body, { status: cacheResponse.status, statusText: cacheResponse.statusText, headers }));
          return response;
        });
      }).catch(() => caches.open(READ_CACHE_NAME).then((cache) => cache.match(request).then((cached) => {
        if (!cached) return caches.match("/index.html");
        const headers = new Headers(cached.headers);
        headers.set("X-WA-Offline", "true");
        return cached.clone().arrayBuffer().then((body) => new Response(body, { status: cached.status, statusText: cached.statusText, headers }));
      })),
    );
    return;
  }
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(request).catch(() => caches.match(request).then((cached) => cached || caches.match("/index.html"))),
  );
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data?.json() ?? {};
  } catch {
    payload = {};
  }
  const title = typeof payload.title === "string" ? payload.title.slice(0, 80) : "观影助手通知";
  const body = typeof payload.body === "string" ? payload.body.slice(0, 240) : "有新的事项需要查看。";
  const url = safeDeepLink(payload.url) || "/notifications";
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag: typeof payload.notification_id === "string" ? payload.notification_id.slice(0, 100) : "watch-assistant",
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = safeDeepLink(event.notification.data?.url) || "/notifications";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      const existing = clients.find((client) => "focus" in client);
      if (existing) {
        // 导航失败(如页面不可导航)时静默忽略,避免未处理的 Promise rejection
        return existing.navigate(target).then((client) => client?.focus()).catch(() => {});
      }
      return self.clients.openWindow(target);
    }),
  );
});

function safeDeepLink(candidate) {
  if (typeof candidate !== "string") return null;
  try {
    const parsed = new URL(candidate, self.location.origin);
    if (parsed.origin !== self.location.origin) return null;
    const path = parsed.pathname + parsed.search + parsed.hash;
    return INTERNAL_DEEP_LINK.test(path) ? path : null;
  } catch {
    return null;
  }
}
