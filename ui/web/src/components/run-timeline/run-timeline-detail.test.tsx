import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineMessages, RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";
import { api } from "@/lib/transport/api";

vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimelineMessages: vi.fn(() => new Promise<never>(() => undefined)), getRunTimelineLinkContent: vi.fn() },
}));

import { NodeDetail, UnitDetail } from "./run-timeline-detail";
import { LinkDetail } from "./run-timeline-link-detail";
import type { ResolvedLink } from "./model/timeline-links";

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

describe("the details of an arrow and of the block it ends on", () => {
  const T = "2026-10-04T12:00:00Z";
  const block = {
    kind: "inbound",
    i0: 4,
    i1: 4,
    start: T,
    end: T,
    source: "agent:405",
    inbound_id: 31,
    preview: "do the thing",
    parent: null,
    context_tokens: 20,
    generation_tokens: null,
    estimated: false,
    session: 0,
    context_total: 20,
    request: null,
  } as RunTimelineUnit;
  const message: RunTimelineMessages = {
    messages: [
      {
        idx: 4,
        ts: T,
        source: "agent:405",
        parts: [{ kind: "inbound", chars: 30, text: "## Plan\n\n- one\n- two\n\n**bold** `code`", text_truncated: false }],
        context_tokens: 20,
        estimated: false,
      },
    ],
    next_start: null,
  };
  const resolved = (over: Partial<ResolvedLink>): ResolvedLink => ({
    key: "k",
    kind: "send_message",
    link: { kind: "send_message", ts: T, sender: 405, receiver: 6657, inbound_id: 31, fork_from: null, notice_id: null },
    from: { row: "units", agent: 405, ms: Date.parse(T) },
    to: { row: "units", agent: 6657, ms: Date.parse(T) },
    external: null,
    unmatched: false,
    block,
    userSource: null,
    ...over,
  });
  const messagesOf = async (ui: React.ReactElement) => {
    cleanup();
    render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
    return (await screen.findAllByTestId("run-timeline-message")).map((el) => el.outerHTML);
  };

  it("render the message with the same DOM, through the same row component", async () => {
    vi.mocked(api.getRunTimelineMessages).mockResolvedValue(message);
    const viaBlock = await messagesOf(<UnitDetail agentId={6657} unit={block} />);
    const viaArrow = await messagesOf(<LinkDetail resolved={resolved({})} onAddAgent={() => undefined} />);
    expect(viaArrow).toEqual(viaBlock);
    expect(viaBlock[0]).toContain("Plan");
    expect(api.getRunTimelineMessages).toHaveBeenCalledWith(6657, expect.objectContaining({ start: 4, end: 4 }));
  });

  it("shows an event with no block through the same card and row, as a message", async () => {
    vi.mocked(api.getRunTimelineMessages).mockClear();
    vi.mocked(api.getRunTimelineLinkContent).mockResolvedValue({ title: "a title", content: "need **a** decision\n\n" + "long ".repeat(500) });
    const none = resolved({ block: null, kind: "notice", link: { ...resolved({}).link, inbound_id: null, notice_id: 3, receiver: null, kind: "notice" } });
    const shown = await messagesOf(<LinkDetail resolved={none} onAddAgent={() => undefined} />);
    expect(shown).toHaveLength(1);
    expect(shown[0]).toContain("<strong");
    // The whole text goes to the row, not a cut of it; the row folds what is long.
    expect(shown[0]).toContain("long long long");
    expect(api.getRunTimelineLinkContent).toHaveBeenCalledWith({ notice_id: 3 });
    expect(api.getRunTimelineMessages).not.toHaveBeenCalled();
  });
});
