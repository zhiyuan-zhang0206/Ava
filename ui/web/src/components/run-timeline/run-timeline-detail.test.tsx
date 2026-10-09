import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineNode } from "@/lib/contracts/types";

vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimelineMessages: vi.fn(() => new Promise<never>(() => undefined)) },
}));

import { NodeDetail } from "./run-timeline-detail";

afterEach(cleanup);

const node = (summary: string): RunTimelineNode => ({
  id: "n1",
  level: 1,
  parent: null,
  start: "2026-10-01T00:00:00Z",
  end: "2026-10-01T01:00:00Z",
  span_start: 0,
  span_end: 3,
  summary,
  usage: { calls: 1, input: 10, cache_read: 0, output: 5, cache_write: 0, cost_usd: 0, cost_calls: 0 },
  generation: null,
  context_tokens: 1500,
  estimated: true,
});

function renderNode(summary: string, usage?: Partial<RunTimelineNode["usage"]>) {
  const base = node(summary);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NodeDetail agentId={1} node={{ ...base, usage: { ...base.usage, ...usage } }} />
    </QueryClientProvider>,
  );
  return screen.getByTestId("run-timeline-summary");
}

describe("NodeDetail summary", () => {
  it("shows the node's context tokens, marked when estimated", () => {
    renderNode("s");
    expect(screen.getByTestId("run-timeline-detail-tokens").textContent).toBe("~1.5k tokens");
  });

  it("shows the recorded cost, and says unknown rather than estimating when none is recorded", () => {
    renderNode("s", { calls: 2, cost_usd: 0.0123, cost_calls: 2, cache_write: 1200 });
    expect(screen.getByText("$0.0123")).toBeTruthy();
    cleanup();
    renderNode("s");
    expect(screen.queryByText("$0.0000")).toBeNull();
  });

  it("renders Markdown structure", () => {
    const el = renderNode("## Title\n\n- one\n- two\n\n**bold** and `code`");
    expect(el.querySelector("h2")?.textContent).toBe("Title");
    expect(el.querySelectorAll("li")).toHaveLength(2);
    expect(el.querySelector("strong")?.textContent).toBe("bold");
    expect(el.querySelector("code")?.textContent).toBe("code");
  });

  it("does not render raw HTML and opens links in a new tab", () => {
    const el = renderNode("<script>x</script>\n\n[ava](https://example.com)");
    expect(el.querySelector("script")).toBeNull();
    const a = el.querySelector("a");
    expect(a?.getAttribute("target")).toBe("_blank");
    expect(a?.getAttribute("rel")).toContain("noopener");
  });
});
