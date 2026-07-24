import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import LibraryView from "../src/views/LibraryView.vue";

describe("LibraryView", () => {
  it("emits selected genre while preserving sort", async () => {
    const wrapper = mount(LibraryView, {
      props: {
        movies: [],
        loading: false,
        favoriteIds: new Set<number>(),
        sort: "popular",
      },
    });

    await wrapper.findAll(".filter-row")[0].findAll("button")[1].trigger("click");

    expect(wrapper.emitted("filters")?.[0]).toEqual([
      { genreId: 28, year: undefined, sort: "popular" },
    ]);
  });
});
