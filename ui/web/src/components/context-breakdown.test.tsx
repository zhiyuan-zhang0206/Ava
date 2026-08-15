// ContextButton / breakdown panel tests — the readout is a button that expands
// an anchored panel in place (NOT a modal dialog), lazily loading the
// per-category context breakdown; failure renders inside the panel (classified
// server-error vs unreachable, with Retry); empty data gets explicit copy.
//
// The component is controlled (the Composer owns the single popover state);
// the harness below supplies the state a la the composer.
//
// happy-dom + RTL — vitest globals=false; explicit cleanup.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { ContextButton, type ContextButtonProps } from "./context-breakdown";

vi.mock("@/lib/api", () => ({
  api: { getContextBreakdown: vi.fn(), getSettings: vi.fn() },
}));

const getContextBreakdown = vi.mocked(api.getContextBreakdown);
const getSettings = vi.mocked(api.getSettings);
// display.context_meter_width unset — useUserSettings falls back to its
// USER_SETTING_DEFAULTS entry ("comfortable"), same as an unconfigured user.
getSettings.mockResolvedValue({ settings: [] });

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const breakdown = {
  total_input_tokens: 1000,
  estimated_total: 250,
  max_input_tokens: 1_000_000,
  soft_compact_tokens: 600_000,
  hard_compact_tokens: 800_000,
  sections: [
    { name: "(preamble)", tokens: 100 },
    { name: "Tools", tokens: 400 },
  ],
  categories: [
    { kind: "system_prompt", tokens: 400 },
    { kind: "output", tokens: 300 },
    { kind: "reasoning", tokens: 100 },
    { kind: "agent_messages", tokens: 150 },
    { kind: "automation", tokens: 50 },
  ],
};

// The endpoint's tolerated degenerate shape: no checkpoint yet.
const emptyBreakdown = {
  total_input_tokens: 0,
  estimated_total: 0,
  max_input_tokens: 0,
  soft_compact_tokens: 0,
  hard_compact_tokens: 0,
  sections: [],
  categories: [],
};

const meterProps = {
  contextTokens: 26_000,
  maxContextTokens: 1_000_000,
  softCompactTokens: 600_000,
  hardCompactTokens: 800_000,
};

/** Owns the controlled open state the way the Composer does. */
function Harness(props: Omit<ContextButtonProps, "open" | "onOpenChange">) {
  const [open, setOpen] = useState(false);
  return <ContextButton {...props} open={open} onOpenChange={setOpen} />;
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ContextButton", () => {
  it("without an agent renders a non-interactive meter (no button)", () => {
    wrap(<Harness agentId={null} {...meterProps} />);
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.getByTestId("context-meter")).toBeTruthy();
  });

  it("with an agent, clicking expands an anchored panel (not a modal) with the breakdown", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);

    // Collapsed: the panel has not mounted, so no fetch yet.
    expect(getContextBreakdown).not.toHaveBeenCalled();
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    const button = screen.getByTestId("context-meter-button");
    expect(button.textContent).toContain("Context: 26.0k/1.00M");

    fireEvent.click(button);

    // Expanding lazily fetches this agent's breakdown and renders it in place.
    await waitFor(() => expect(getContextBreakdown).toHaveBeenCalledWith(7));
    await screen.findByTestId("context-breakdown-categories");
    expect(screen.getByTestId("context-breakdown-panel")).toBeTruthy();
    // No modal semantics: no dialog role, and the trigger stays visible.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("context-meter-button")).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("System prompt")).toBeTruthy();
    expect(screen.getByText("Text output")).toBeTruthy();
    expect(screen.getByText("Reasoning")).toBeTruthy();
    // The inbound-source split renders its own labelled legend rows.
    expect(screen.getByText("Agent messages")).toBeTruthy();
    expect(screen.getByText("System notes")).toBeTruthy();
    // The section block is collapsed by default — only its header shows until
    // the chevron is clicked; then the rows render.
    expect(screen.queryByText("(preamble)")).toBeNull();
    fireEvent.click(screen.getByTestId("context-breakdown-sections-toggle"));
    expect(screen.getByText("(preamble)")).toBeTruthy();
    expect(screen.getByText("Tools")).toBeTruthy();
  });

  it("drills a recursive section: children are collapsed by default and expand on click", async () => {
    getContextBreakdown.mockResolvedValue({
      ...breakdown,
      sections: [
        { name: "(preamble)", tokens: 100 },
        {
          name: "expanded SDK reference",
          tokens: 3000,
          children: [
            { name: "(intro)", tokens: 200 },
            { name: "ava.self", tokens: 1500 },
            { name: "ava.ui", tokens: 1300 },
          ],
        },
      ],
    });
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    // The section block is collapsed by default — open it, then drill.
    await screen.findByTestId("context-breakdown-categories");
    fireEvent.click(screen.getByTestId("context-breakdown-sections-toggle"));
    await screen.findByTestId("context-breakdown-sections");

    // Rows are collapsed by default: the parent row shows, its children do not.
    expect(screen.getByText("expanded SDK reference")).toBeTruthy();
    expect(screen.queryByText("ava.self")).toBeNull();

    // Clicking the disclosure toggle drills in.
    fireEvent.click(screen.getByRole("button", { name: "Expand expanded SDK reference" }));
    expect(screen.getByText("ava.self")).toBeTruthy();
    expect(screen.getByText("ava.ui")).toBeTruthy();
    expect(screen.getByText("(intro)")).toBeTruthy();

    // The chevron sits flush against the label — no gap between button and text.
    const sdkRow = screen.getByText("expanded SDK reference").closest("div")!;
    expect(sdkRow.className).not.toContain("gap-");

    // Leaf sections (no children) have no toggle.
    expect(screen.queryByRole("button", { name: /Expand \(preamble\)/ })).toBeNull();
  });

  it("clicking the meter again collapses the panel", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    const button = screen.getByTestId("context-meter-button");
    fireEvent.click(button);
    await screen.findByTestId("context-breakdown-panel");
    fireEvent.click(button);
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("the close button and Escape both collapse the panel", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    fireEvent.click(screen.getByTestId("context-breakdown-close"));
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();

    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
  });

  it("ignores an Escape another layer already consumed (defaultPrevented)", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    // E.g. the composer's textarea handler closing the slash dropdown calls
    // preventDefault — that Escape must not also collapse this panel.
    const consumed = new KeyboardEvent("keydown", { key: "Escape", cancelable: true });
    consumed.preventDefault();
    document.dispatchEvent(consumed);
    expect(screen.getByTestId("context-breakdown-panel")).toBeTruthy();
  });

  it("classifies an HTTP failure as a server error, inside the panel", async () => {
    getContextBreakdown.mockRejectedValue(new Error("HTTP 500: Internal Server Error"));
    wrap(<Harness agentId={9} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const err = await screen.findByTestId("context-breakdown-error");
    expect(err.textContent).toContain("the server returned an error");
    expect(err.textContent).toContain("HTTP 500: Internal Server Error");
    // The failure stays in the expanded panel — no toast, still collapsible.
    expect(screen.getByTestId("context-breakdown-panel")).toBeTruthy();
  });

  it("classifies a network failure as gateway-unreachable and Retry refetches", async () => {
    getContextBreakdown.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    getContextBreakdown.mockResolvedValueOnce(breakdown);
    wrap(<Harness agentId={9} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const err = await screen.findByTestId("context-breakdown-error");
    expect(err.textContent).toContain("could not reach the gateway");
    expect(err.textContent).toContain("Failed to fetch");

    fireEvent.click(screen.getByTestId("context-breakdown-retry"));
    await screen.findByTestId("context-breakdown-categories");
    expect(getContextBreakdown).toHaveBeenCalledTimes(2);
  });

  it("renders an explicit no-data state for the empty breakdown (no bare '0 tokens')", async () => {
    getContextBreakdown.mockResolvedValue(emptyBreakdown);
    wrap(<Harness agentId={3} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const empty = await screen.findByTestId("context-breakdown-empty");
    expect(empty.textContent).toContain("No context recorded yet");
    expect(screen.queryByTestId("context-breakdown-total")).toBeNull();
    expect(screen.queryByTestId("context-breakdown-categories")).toBeNull();
  });

  it("the expanded panel is shrink-to-fit width (min/max-bounded, not a hard fixed width), anchored to the left, not stretched full-width", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    // min-w-80 is a floor (a sparse breakdown isn't a sliver); max-w-[...] is
    // a ceiling (one pathologically long name can't push the panel past the
    // viewport). Between those bounds width is content-driven — no `w-80`
    // (or any other bare `w-*`) forcing a fixed width regardless of content.
    const classes = panel.className.split(/\s+/);
    expect(classes).toContain("min-w-80");
    expect(classes.some((c) => c.startsWith("max-w-["))).toBe(true);
    expect(classes).not.toContain("w-80");
    expect(panel.className).not.toContain("right-0");
    expect(panel.className).toContain("left-0");
  });

  it("resets the inherited white-space: nowrap from the composer's truncate meta-row span, so panel text wraps instead of clipping", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    // The composer wraps this panel in a `truncate` span for the collapsed
    // meter's own text; `white-space` is inherited, so without this reset
    // every non-truncate paragraph in the panel (the summary line, the
    // description) would refuse to wrap and get clipped by overflow-x-hidden
    // instead of flowing across lines like the rest of the panel.
    expect(panel.className).toContain("whitespace-normal");
  });

  it("applies the display.context_meter_width setting to the collapsed meter's gauge track", async () => {
    getSettings.mockResolvedValueOnce({
      settings: [
        { key: "display.context_meter_width", value: "wide", updated_at: "2026-01-01T00:00:00Z" },
      ],
    });
    wrap(<Harness agentId={null} {...meterProps} />);
    await waitFor(() => expect(screen.getByRole("img").className).toContain("w-48"));
  });

  it("the panel clips horizontal overflow instead of scrolling — overflow-x-hidden paired with overflow-y-auto", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    // Per the CSS overflow spec, an element with overflow-y other than
    // "visible" forces its overflow-x's *computed* value from "visible" to
    // "auto" too (the two axes aren't independent that way) — so
    // overflow-y-auto alone silently made this panel horizontally scrollable
    // once content ran past the max-width ceiling. overflow-x-hidden pins
    // the other axis explicitly so content clips / truncates in place
    // instead of demanding a horizontal scroll.
    expect(panel.className).toContain("overflow-x-hidden");
    expect(panel.className).toContain("overflow-y-auto");
  });

  it("legend percentages carry 2 decimal places so small categories stay readable (no all-zero legend)", async () => {
    // 1000 tokens total: system_prompt 400 -> 40.00%, tool_response 299 ->
    // 29.90%, compact_summary 1 -> 0.10% — the 2-decimal precision the user
    // picked after the 3-decimal release (small categories must not read "0%").
    getContextBreakdown.mockResolvedValue({
      ...breakdown,
      categories: [
        { kind: "system_prompt", tokens: 400 },
        { kind: "compact_summary", tokens: 1 },
        { kind: "output", tokens: 300 },
        { kind: "tool_response", tokens: 299 },
      ],
    });
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");
    // The label and the value are sibling spans in one row, so read the <li>.
    const row = (label: string) => screen.getByText(label).closest("li")!.textContent;
    expect(row("Compact summary")).toContain("0.10%");
    expect(row("Tool responses")).toContain("29.90%");
    expect(row("System prompt")).toContain("40.00%");
  });

  it("legend rows sort by context share, largest first (not the backend's fixed order)", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");
    // 400 / 300 / 150 / 100 / 50 tokens -> descending. The sort is stable, so
    // equal shares would keep the backend's canonical CATEGORY_ORDER.
    const rows = [...screen
      .getByTestId("context-breakdown-categories")
      .querySelectorAll("li")];
    expect(rows.map((r) => r.textContent)).toEqual([
      "System prompt400 · 40.00%",
      "Text output300 · 30.00%",
      "Agent messages150 · 15.00%",
      "Reasoning100 · 10.00%",
      "System notes50 · 5.00%",
    ]);
  });

  it("merges context_note + automation into one 'System notes' row (summed tokens)", async () => {
    getContextBreakdown.mockResolvedValue({
      ...breakdown,
      categories: [
        { kind: "system_prompt", tokens: 400 },
        { kind: "context_note", tokens: 30 },
        { kind: "output", tokens: 300 },
        { kind: "automation", tokens: 50 },
      ],
    });
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");
    // 30 + 50 = 80 tokens -> one "System notes" row at 8.00%; the two API
    // kinds must not render as separate rows.
    const rows = [...screen
      .getByTestId("context-breakdown-categories")
      .querySelectorAll("li")];
    expect(rows.map((r) => r.textContent)).toEqual([
      "System prompt400 · 40.00%",
      "Text output300 · 30.00%",
      "System notes80 · 8.00%",
    ]);
    expect(screen.queryByText("Context notes")).toBeNull();
  });

  it("legend values render as one 'tokens · pct%' group — dot with exactly one space each side, tabular", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");
    // The row's trailing span is a single group: "400 · 40.00%" — the · is
    // back with one space on each side (no wide fixed-width columns, no dot
    // dropped), in tabular figures so values align across rows.
    const row = screen.getByText("System prompt").closest("li")!;
    const group = row.querySelector(":scope > span:last-child")!;
    expect(group.textContent).toBe("400 · 40.00%");
    expect(group.className).toContain("tabular-nums");
  });

  it("splits the occupancy summary into two lines: tokens, then wind-down · auto-compact", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-total");
    const total = screen.getByTestId("context-breakdown-total");
    const lines = [...total.querySelectorAll("span.block")];
    expect(lines.map((l) => l.textContent)).toEqual([
      "1.0k / 1.00M tokens",
      "wind-down 600.0k · auto-compact 800.0k",
    ]);
  });

  it("the whole section block is collapsed by default and disclosed by its own chevron (same as a section row)", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");

    // The block header shows with a collapsed (un-rotated) chevron; the rows
    // underneath are not rendered at all.
    const toggle = screen.getByTestId("context-breakdown-sections-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("context-breakdown-sections")).toBeNull();

    // Clicking the chevron discloses the whole block, like a section row.
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("context-breakdown-sections")).toBeTruthy();
    expect(screen.getByText("(preamble)")).toBeTruthy();
    expect(screen.getByText("Tools")).toBeTruthy();

    // Clicking again collapses the whole block.
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("context-breakdown-sections")).toBeNull();
  });

  it("leaf section rows have no spacer — their label is the row's first element (same left edge as sibling chevrons)", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<Harness agentId={7} {...meterProps} />);
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-categories");
    fireEvent.click(screen.getByTestId("context-breakdown-sections-toggle"));
    await screen.findByTestId("context-breakdown-sections");
    // "(preamble)" is a leaf: its row must start with the label itself, not a
    // 12px spacer, so the text left edge matches expandable rows' chevron edge.
    const leafLabel = screen.getByText("(preamble)");
    const row = leafLabel.closest("div")!;
    expect(row.firstElementChild).toBe(leafLabel);
  });
});
