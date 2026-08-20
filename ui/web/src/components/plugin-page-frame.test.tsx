// PluginPageFrame — the embed contract.
//
// Three properties are load-bearing and none of them is visible in a
// screenshot: the frame points at the plugin's own gateway mount (not at some
// URL the declaration supplied), it is sandboxed, and the sandbox withholds
// top-level navigation — a plugin page must not be able to steer the console
// away from itself.

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PluginPageFrame } from "@/components/plugin-page-frame";

vi.mock("@/lib/api", () => ({ API_BASE: "http://gateway.test:8000" }));

afterEach(cleanup);

function frame(page: string): HTMLIFrameElement {
  const { container } = render(
    <PluginPageFrame plugin="board" page={page} title="Task board" />,
  );
  const el = container.querySelector("iframe");
  expect(el).not.toBeNull();
  return el!;
}

describe("PluginPageFrame", () => {
  it("loads the page from the plugin's own gateway mount", () => {
    expect(frame("board/").getAttribute("src")).toBe(
      "http://gateway.test:8000/api/plugin-ui/board/board/",
    );
  });

  it("percent-encodes each path segment without eating the separators", () => {
    expect(frame("a b/c.html").getAttribute("src")).toBe(
      "http://gateway.test:8000/api/plugin-ui/board/a%20b/c.html",
    );
  });

  it("is sandboxed, and the sandbox withholds top-level navigation", () => {
    const sandbox = frame("board/").getAttribute("sandbox") ?? "";
    const tokens = sandbox.split(" ").filter(Boolean);
    expect(tokens).toContain("allow-scripts");
    // A plugin page steering the whole console away would be the one breakage
    // the containment boundary exists to prevent.
    expect(tokens).not.toContain("allow-top-navigation");
    expect(tokens).not.toContain("allow-top-navigation-by-user-activation");
    expect(tokens).not.toContain("allow-downloads");
  });
});
