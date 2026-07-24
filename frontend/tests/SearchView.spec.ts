import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import SearchView from "../src/views/SearchView.vue";

describe("SearchView", () => {
  it("renders popular movies and emits the selected movie", async () => {
    const movie = {
      tmdb_id: 27205,
      title: "盗梦空间",
      original_title: "Inception",
      release_year: 2010,
      overview: "Dreams within dreams.",
      poster_path: "/poster.jpg",
      vote_average: 8.4,
    };
    const wrapper = mount(SearchView, {
      props: {
        modelValue: "",
        loading: false,
        movies: [movie],
        heading: "搜索结果",
        favoriteIds: new Set<string>(),
        page: 1,
        totalPages: 3,
        totalResults: 55,
      },
    });

    await wrapper.get('button[aria-label="查看 盗梦空间"]').trigger("click");

    expect(wrapper.get("h1").text()).toBe("搜索结果");
    expect(wrapper.emitted("open")?.[0]).toEqual([movie]);
    await wrapper.get('button[aria-label="下一页"]').trigger("click");
    expect(wrapper.emitted("page")?.[0]).toEqual([2]);
  });
});
