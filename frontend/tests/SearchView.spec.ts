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
        heading: "当前热门",
      },
    });

    await wrapper.get('button[aria-label="查看 盗梦空间"]').trigger("click");

    expect(wrapper.get("h2").text()).toBe("当前热门");
    expect(wrapper.emitted("open")?.[0]).toEqual([movie]);
  });
});
