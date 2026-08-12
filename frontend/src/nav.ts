import { Bell, ClipboardCheck, Clock3, Database, FileText, Film, Flame, Heart, Home, ListTodo, Search, Settings, Tv } from "@lucide/vue";
import type { Component } from "vue";
import type { BrowseView } from "./router";

export interface NavItem {
  view: BrowseView;
  label: string;
  icon: Component;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/** 侧栏导航分组：发现 / 我的 / 管理。organization 项按能力开关有条件显示。 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "发现",
    items: [
      { view: "home", label: "首页", icon: Home },
      { view: "movies", label: "电影", icon: Film },
      { view: "tv", label: "剧集", icon: Tv },
      { view: "popular", label: "热门", icon: Flame },
      { view: "search", label: "搜索", icon: Search },
    ],
  },
  {
    label: "我的",
    items: [
      { view: "favorites", label: "收藏", icon: Heart },
      { view: "history", label: "记录", icon: Clock3 },
    ],
  },
  {
    label: "管理",
    items: [
      { view: "library", label: "媒体库", icon: Database },
      { view: "organization", label: "整理", icon: ClipboardCheck },
      { view: "workflows", label: "任务中心", icon: ListTodo },
      { view: "notifications", label: "通知", icon: Bell },
      { view: "logs", label: "日志", icon: FileText },
      { view: "settings", label: "设置", icon: Settings },
    ],
  },
];

export const ALL_NAV_VIEWS: BrowseView[] = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view));
