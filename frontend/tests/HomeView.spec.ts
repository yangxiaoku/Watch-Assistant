import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import HomeView from "../src/views/HomeView.vue";

const movie = {
  tmdb_id: 27205,
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
        },
        loading: false,
        favoriteIds: new Set<number>(),
      },
    });

    expect(wrapper.get(".feature-hero").text()).toContain("盗梦空间");
    expect(wrapper.text()).toContain("正在热映");
    expect(wrapper.text()).toContain("即将上映");
    await wrapper.get('.feature-hero').trigger("click");
    expect(wrapper.emitted("open")?.[0]).toEqual([movie]);
  });
});
