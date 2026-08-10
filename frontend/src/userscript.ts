// ==UserScript==
// @name         Watch Assistant TMDB Panel
// @namespace    local.watch-assistant
// @match        https://www.themoviedb.org/movie/*
// @grant        GM.xmlHttpRequest
// @grant        GM.getValue
// @grant        GM.setValue
// @connect      *
// ==/UserScript==

import panelStyles from "./userscript.css?raw";
import type { ResourceSummary, SearchResponse } from "./types";

const SEARCH_TTL = 10 * 60_000;
const API_BASE_KEY = "watch-assistant-api-base";
const TOKEN_KEY = "watch-assistant-token";

type GmApi = {
  getValue<T>(key: string, fallback: T): Promise<T>;
  setValue<T>(key: string, value: T): Promise<void>;
  xmlHttpRequest(details: {
    method: string;
    url: string;
    headers?: Record<string, string>;
    data?: string;
    onload: (response: { status: number; responseText: string }) => void;
    onerror: () => void;
    ontimeout: () => void;
  }): void;
};

declare const GM: GmApi | undefined;

export function extractMovieId(path: string): number | null {
  const match = path.match(/^\/movie\/(\d+)(?:-|\/|$)/);
  return match ? Number(match[1]) : null;
}

export function shouldSearchMovie(
  movieId: number,
  cache: Map<number, number>,
  now = Date.now(),
): boolean {
  const lastSearch = cache.get(movieId);
  return lastSearch === undefined || now - lastSearch >= SEARCH_TTL;
}

/** Map a known failure to an actionable message without echoing arbitrary error
 * text (which may contain URLs, tokens, or paths). Unknown errors fall back to
 * the generic redacted message. */
export function formatUserscriptError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (message === "network error" || message === "timeout") {
    return "无法连接 Watch Assistant 服务，请检查 API 地址是否可达、服务是否已启动。";
  }
  if (message === "request failed") {
    return "服务拒绝了请求，请检查脚本设置中的 Token 与 API 地址。";
  }
  if (message === "GM API unavailable") {
    return "缺少 GM API 权限，请确认已安装 Tampermonkey 且未阻止本脚本。";
  }
  return "观影资源暂时不可用，请稍后重试。";
}

function gmRequest<T>(url: string, token: string, init: { method: string; body?: unknown } = { method: "GET" }): Promise<T> {
  return new Promise((resolve, reject) => {
    GM?.xmlHttpRequest({
      method: init.method,
      url,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      data: init.body ? JSON.stringify(init.body) : undefined,
      onload: (response) => {
        try {
          const body = JSON.parse(response.responseText);
          if (response.status < 200 || response.status >= 300) reject(new Error("request failed"));
          else resolve(body as T);
        } catch { reject(new Error("invalid response")); }
      },
      onerror: () => reject(new Error("network error")),
      ontimeout: () => reject(new Error("timeout")),
    }) ?? reject(new Error("GM API unavailable"));
  });
}

function renderPanel(shadow: ShadowRoot, state: { title: string; result?: SearchResponse; error?: string; busy?: string }) {
  const resources = state.result?.results ?? [];
  const body = state.error
    ? `<p class="error">${escapeHtml(state.error)}</p>`
    : state.result
      ? (resources.length ? resources.map((resource) => resourceRow(resource, state.busy)).join("") : `<p class="empty">没有找到可用资源</p>`)
      : `<p class="loading">正在搜索可用资源…</p>`;
  shadow.innerHTML = `<style>${panelStyles}</style><section class="panel"><header><div><p class="eyebrow">WATCH ASSISTANT</p><h2>${escapeHtml(state.title)}</h2></div><button class="close" aria-label="关闭">×</button></header><div class="panel-body">${body}</div></section>`;
  shadow.querySelector(".close")?.addEventListener("click", () => shadow.host.remove());
  shadow.querySelectorAll<HTMLButtonElement>("button[data-resource-id]").forEach((button) => {
    button.addEventListener("click", () => void pushResource(button, button.dataset.resourceId ?? ""));
  });
}

function renderConfigPanel(shadow: ShadowRoot, onSaved: () => void) {
  shadow.innerHTML = `<style>${panelStyles}</style><section class="panel"><header><div><p class="eyebrow">WATCH ASSISTANT</p><h2>连接个人服务</h2></div><button class="close" aria-label="关闭">×</button></header><div class="panel-body"><label>API 地址</label><input class="config-input" data-api-base placeholder="http://192.168.6.236:8000"><label>脚本 Token</label><input class="config-input" data-token type="password" placeholder="Bearer Token"><button class="push" data-save>保存并搜索</button></div></section>`;
  shadow.querySelector(".close")?.addEventListener("click", () => shadow.host.remove());
  shadow.querySelector<HTMLButtonElement>("button[data-save]")?.addEventListener("click", async () => {
    const base = shadow.querySelector<HTMLInputElement>("input[data-api-base]")?.value.trim() ?? "";
    const token = shadow.querySelector<HTMLInputElement>("input[data-token]")?.value.trim() ?? "";
    if (!base || !token || !GM) return;
    await Promise.all([GM.setValue(API_BASE_KEY, base), GM.setValue(TOKEN_KEY, token)]);
    onSaved();
  });
}

function resourceRow(resource: ResourceSummary, busy?: string): string {
  const size = resource.size_bytes === null ? "未知" : formatSize(resource.size_bytes);
  const seeders = resource.seeders === null ? "未知" : String(resource.seeders);
  return `<article class="resource"><div class="resource-name">${escapeHtml(resource.name)}</div><div class="meta"><span class="kind">${resource.kind === "magnet" ? "磁力" : "115 分享"}</span><span>${escapeHtml(resource.source)}</span><span>${size} · 做种 ${seeders}</span></div><button class="push" data-resource-id="${escapeHtml(resource.resource_id)}" ${busy === resource.resource_id ? "disabled" : ""}>${busy === resource.resource_id ? "推送中" : "推送到 115"}</button></article>`;
}

async function pushResource(button: HTMLButtonElement, resourceId: string) {
  const base = await GM?.getValue(API_BASE_KEY, "") ?? "";
  const token = await GM?.getValue(TOKEN_KEY, "") ?? "";
  if (!base || !token) return;
  button.disabled = true;
  button.textContent = "推送中";
  try { await gmRequest(`${base.replace(/\/$/, "")}/api/v1/tasks`, token, { method: "POST", body: { resource_id: resourceId } }); button.textContent = "已提交"; } catch { button.disabled = false; button.textContent = "重试推送"; }
}

function formatSize(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value; let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>\"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[character] ?? character);
}

function installUserscript() {
  if (typeof GM === "undefined") return;
  const searchCache = new Map<number, number>();
  let panelHost: HTMLElement | null = null;
  let shadow: ShadowRoot | null = null;
  let debounce: number | undefined;

  const load = async () => {
    const movieId = extractMovieId(window.location.pathname);
    if (!movieId || !shouldSearchMovie(movieId, searchCache)) return;
    panelHost?.remove();
    panelHost = document.createElement("div");
    panelHost.id = "watch-assistant-tmdb-panel";
    panelHost.style.cssText = "position:fixed;right:20px;top:84px;z-index:2147483647";
    shadow = panelHost.attachShadow({ mode: "open" });
    document.body.appendChild(panelHost);
    const base = await GM.getValue(API_BASE_KEY, "");
    const token = await GM.getValue(TOKEN_KEY, "");
    // 读取配置期间已导航到其他影片:不再为旧影片渲染面板
    if (extractMovieId(window.location.pathname) !== movieId) return;
    if (!base || !token) {
      renderConfigPanel(shadow, () => void load());
      return;
    }
    renderPanel(shadow, { title: "搜索中" });
    try {
      const result = await gmRequest<SearchResponse>(`${base.replace(/\/$/, "")}/api/v1/search`, token, { method: "POST", body: { tmdb_id: movieId } });
      // Only remember the search once it succeeded; a failed attempt must not
      // suppress the panel for the whole ten-minute TTL.
      searchCache.set(movieId, Date.now());
      // 搜索在途时已导航到其他影片:丢弃过期响应,避免渲染进当前影片的面板
      if (extractMovieId(window.location.pathname) !== movieId) return;
      renderPanel(shadow, { title: result.movie.title, result });
    } catch (error) {
      if (extractMovieId(window.location.pathname) !== movieId) return;
      renderPanel(shadow, { title: "资源面板", error: formatUserscriptError(error) });
    }
  };
  const schedule = () => { window.clearTimeout(debounce); debounce = window.setTimeout(() => void load(), 300); };
  window.addEventListener("popstate", schedule);
  const pushState = history.pushState;
  history.pushState = function (...args) { pushState.apply(this, args); schedule(); };
  const replaceState = history.replaceState;
  history.replaceState = function (...args) { replaceState.apply(this, args); schedule(); };
  void load();
}

if (typeof window !== "undefined") installUserscript();
