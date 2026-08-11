import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import PosterCard from "../src/components/PosterCard.vue";

const movie = {
  tmdb_id: 1,
  media_type: "movie" as const,
  title: "盗梦空间",
  original_title: "Inception",
  release_year: 2010,
  overview: "",
  poster_path: null,
  backdrop_path: null,
  genre_ids: [],
  vote_average: 8.4,
};

describe("PosterCard", () => {
  it("渲染片名与评分小字", () => {
    const wrapper = mount(PosterCard, { props: { movie } });
    expect(wrapper.get("strong").text()).toBe("盗梦空间");
    expect(wrapper.get(".rating-chip").text()).toBe("8.4");
    expect(wrapper.get("button.movie-card").attributes("aria-label")).toBe("查看 盗梦空间");
  });

  it("open 事件携带影片", async () => {
    const wrapper = mount(PosterCard, { props: { movie } });
    await wrapper.get("button.movie-card").trigger("click");
    expect(wrapper.emitted("open")?.[0]).toEqual([movie]);
  });

  it("favorite 事件携带影片", async () => {
    const wrapper = mount(PosterCard, { props: { movie, favorite: true } });
    await wrapper.get("button.favorite-button").trigger("click");
    expect(wrapper.emitted("favorite")?.[0]).toEqual([movie]);
  });
});
