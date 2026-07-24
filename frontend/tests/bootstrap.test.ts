import { expect, test } from "vitest";

test("provides a browser DOM environment", () => {
  const element = document.createElement("div");
  element.textContent = "ready";
  document.body.appendChild(element);

  expect(document.body.textContent).toContain("ready");
});
