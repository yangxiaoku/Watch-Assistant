import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import MovieView from "../src/views/MovieView.vue";

const resource = {
  resource_id: "res_1",
  kind: "magnet" as const,
  name: "Show S01",
  size_bytes: null,
  seeders: null,
  source: "test",
  captured_at: "2026-07-24T10:00:00Z",
};

function response(mediaType: "movie" | "tv") {
  return {
    movie: {
      tmdb_id: 1399,
      media_type: mediaType,
      title: "权力的游戏",
      original_title: "Game of Thrones",
      release_year: 2011,
      overview: null,
      poster_path: null,
      vote_average: 8.2,
      seasons: [
        { season_number: 1, name: "第 1 季", episode_count: 10, air_date: "2011-04-17", poster_path: null },
        { season_number: 2, name: "第 2 季", episode_count: 10, air_date: "2012-04-01", poster_path: null },
      ],
    },
    results: [resource],
    warnings: [],
    cached: false,
    cache_age_seconds: null,
  };
}

describe("MovieView seasons", () => {
  it("keeps metadata status separate from the resource surface", () => {
    const wrapper = mount(MovieView, {
      props: {
        result: response("movie"),
        metadataLoading: true,
        resources: [],
        resourceLoading: true,
        mediaType: "movie",
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    expect(wrapper.find(".metadata-status").text()).toContain("正在加载影视资料");
    expect(wrapper.find(".resource-surface").exists()).toBe(true);
    expect(wrapper.find(".movie-profile").attributes("aria-busy")).toBe("true");
  });

  it("offers a metadata retry without changing resource controls", async () => {
    const wrapper = mount(MovieView, {
      props: {
        result: response("movie"),
        metadataError: "上游暂不可用",
        resources: [resource],
        resourceLoading: false,
        mediaType: "movie",
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    await wrapper.get(".metadata-status .text-button").trigger("click");
    expect(wrapper.emitted("retryMetadata")).toHaveLength(1);
    expect(wrapper.find(".resource-table").exists()).toBe(true);
  });

  it("keeps derived stats and the overview in separate detail regions", () => {
    const result = response("tv");
    result.movie.overview = "这是简介内容。";
    const wrapper = mount(MovieView, {
      props: {
        result,
        mediaType: "tv",
        seasonNumber: null,
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    expect(wrapper.find(".detail-stats").text()).toContain("磁力");
    expect(wrapper.find(".detail-stats").text()).toContain("115 分享");
    expect(wrapper.find(".overview-section").text()).toContain("这是简介内容。");
    expect(wrapper.text()).not.toContain("年份未知");
  });

  it("shows all seasons and emits the selected season", async () => {
    const wrapper = mount(MovieView, {
      props: {
        result: response("tv"),
        mediaType: "tv",
        seasonNumber: null,
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    expect(wrapper.get("#season-select").findAll("option")).toHaveLength(3);
    await wrapper.get("#season-select").setValue("2");
    expect(wrapper.emitted("season")).toEqual([[2]]);
  });

  it("does not render a season control for movies", () => {
    const wrapper = mount(MovieView, {
      props: {
        result: response("movie"),
        mediaType: "movie",
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    expect(wrapper.find("#season-select").exists()).toBe(false);
  });

  it("only shows season zero when it is present in metadata", () => {
    const withoutZero = mount(MovieView, {
      props: {
        result: response("tv"),
        mediaType: "tv",
        seasonNumber: 0,
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });
    expect(withoutZero.find('option[value="0"]').exists()).toBe(false);

    const withZero = response("tv");
    withZero.movie.seasons = [
      { season_number: 0, name: "特别篇", episode_count: 1, air_date: "2010-01-01", poster_path: null },
      ...withZero.movie.seasons,
    ];
    const withZeroWrapper = mount(MovieView, {
      props: {
        result: withZero,
        mediaType: "tv",
        seasonNumber: 0,
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });
    expect(withZeroWrapper.get('option[value="0"]').text()).toContain("特别篇");
  });

  it("shows independent season overview instead of the series overview", () => {
    const result = response("tv");
    result.movie.overview = "剧集总简介，不应冒充季度简介";
    const wrapper = mount(MovieView, {
      props: {
        result,
        mediaType: "tv",
        seasonNumber: 2,
        seasonDetail: {
          series_tmdb_id: 1399,
          tmdb_season_id: 456,
          season_number: 2,
          name: "第 2 季",
          overview: "本季独立简介",
          overview_language: "zh-CN",
          poster_path: null,
          air_date: "2012-04-01",
          episode_count: 10,
          vote_average: null,
          source: "tmdb",
          fetched_at: "2026-07-29T00:00:00Z",
          cached: false,
          stale: false,
          data_version: 1,
          episodes: [],
          warnings: [],
        },
        pushingId: null,
        pushCapabilities: { magnet: true, share: true },
        favorite: false,
      },
    });

    expect(wrapper.find(".season-detail-section").text()).toContain("本季独立简介");
    expect(wrapper.find(".season-detail-section").text()).not.toContain("剧集总简介");
  });
});
