import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import HomeView from "../src/views/HomeView.vue";

const movie = {
  tmdb_id: 27205,
  media_type: "movie" as const,
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "梦境中的梦境。",
  poster_path: "/poster.jpg",
  backdrop_path: "/backdrop.jpg",
  genre_ids: [878],
  vote_average: 8.4,
};

describe("HomeView", () => {
  it("renders a real hero and content sections", async () => {
    const wrapper = mount(HomeView, {
      props: {
        catalog: {
          popular: [movie],
          now_playing: [movie],
          upcoming: [movie],
          top_rated: [movie],
          tv_popular: [{ ...movie, tmdb_id: 1399, media_type: "tv" as const, title: "权力的游戏" }],
          tv_on_the_air: [{ ...movie, tmdb_id: 1399, media_type: "tv" as const, title: "权力的游戏" }],
          tv_top_rated: [{ ...movie, tmdb_id: 1399, media_type: "tv" as const, title: "权力的游戏" }],
        },
        loading: false,
        favoriteIds: new Set<string>(),
      },
    });

    expect(wrapper.get(".feature-hero").text()).toContain("盗梦空间");
    expect(wrapper.text()).toContain("正在热映");
    expect(wrapper.text()).toContain("即将上映");
    expect(wrapper.text()).toContain("热播剧集");
    await wrapper.get('.feature-hero').trigger("click");
    expect(wrapper.emitted("open")?.[0]).toEqual([movie]);
  });
});
