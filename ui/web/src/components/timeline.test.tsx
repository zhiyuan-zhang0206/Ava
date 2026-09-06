// timeline.tsx component test — ItemView per-kind render + envelope split +
// system_marker dispatch + LifecycleChip + UnknownMarkerChip +
// InterruptedNotice + CopyButton + ForkButton.
//
// jsdom does not actually render scroll, so scroll behavior is not
// tested (the sticky state machine was extracted to lib/sticky.ts and
// is covered by sticky.test.ts). ChatMarkdown / PythonCode / ScrollArea
// are mocked as stubs to reduce noise and avoid the runtime complexity
// of react-markdown / Prism / base-ui.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/types";

vi.mock("./markdown", () => ({
  ChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="chat-markdown">{content}</div>
  ),
}));

vi.mock("./python-code", () => ({
  PythonCode: ({ code, streaming }: { code: string; streaming?: boolean }) => (
    <div className="group relative">
      <button type="button" aria-label="Copy code">
        Copy
      </button>
      <pre data-testid="python-code" data-streaming={streaming ? "1" : "0"}>
        {code}
      </pre>
    </div>
  ),
}));

vi.mock("./ui/scroll-area", () => ({
  ScrollArea: ({
    children,
    className,
    viewportClassName,
  }: {
    children: React.ReactNode;
    className?: string;
    viewportClassName?: string;
  }) => (
    <div data-testid="scroll-area" className={className}>
      <div
        data-testid="scroll-viewport"
        data-slot="scroll-area-viewport"
        className={viewportClassName}
      >
        {children}
      </div>
    </div>
  ),
}));

// Details mode can be overridden per-test — defaults to "all".
// `setToggleState({ detailsMode: "last" })` changes the mode inside a describe block.
// `isLoading` models the DB-backed settings query still in flight: while true,
// the hook's detailsMode is the USER_SETTING_DEFAULTS fallback ("all") but the
// timeline must render the safe collapsed state (the flash regression).
const toggleState = {
  detailsMode: "all" as "all" | "last" | "none",
  isLoading: false,
};
function setToggleState(patch: Partial<typeof toggleState>) {
  Object.assign(toggleState, patch);
}
function resetToggleState() {
  toggleState.detailsMode = "all";
  toggleState.isLoading = false;
}

vi.mock("@/lib/content-toggle-store", () => ({
  useContentToggle: () => ({
    detailsMode: toggleState.detailsMode,
    setDetailsMode: vi.fn(),
    isLoading: toggleState.isLoading,
  }),
  // Same-mode re-pick reset token (user ruling 2026-08-06): static in tests —
  // TimelineView only clears overrides when the token moves; it never does here.
  useContentToggleReset: (selector?: (s: { resetToken: number; bumpReset: () => void }) => unknown) =>
    selector
      ? selector({ resetToken: 0, bumpReset: vi.fn() })
      : { resetToken: 0, bumpReset: vi.fn() },
}));

// CardHeader now reads user settings via useUserSettings; mock it so the
// timeline component tests don't need a full React Query provider setup.
// resetUserSettings restores the defaults in afterEach.
const userSettingsState: Record<string, unknown> = {
  "display.render_reasoning_markdown": true,
  "display.show_timestamp_weekday": true,
};
function setUserSettings(settings: Record<string, unknown>) {
  Object.assign(userSettingsState, settings);
}
function resetUserSettings() {
  userSettingsState["display.render_reasoning_markdown"] = true;
  userSettingsState["display.show_timestamp_weekday"] = true;
}

vi.mock("@/lib/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: userSettingsState,
    setSetting: vi.fn(),
    isLoading: false,
  }),
}));

import { TimelineView } from "./timeline";
import { streamingParseIntervalMs } from "./timeline/item";
import { LIFECYCLE_TAGS, MEMORY_SOURCES, NOTE_SOURCES } from "./timeline/markers";

afterEach(() => {
  cleanup();
  resetToggleState();
  resetUserSettings();
});

function makeItem(overrides: Partial<BackendTimelineItem> & Pick<BackendTimelineItem, "kind" | "payload">): BackendTimelineItem {
  return {
    item_id: overrides.item_id ?? "1.0",
    kind: overrides.kind,
    payload: overrides.payload,
    source: overrides.source ?? null,
    // `in` rather than `??` so an explicit `created_at: null` survives — the
    // backend really does emit unstamped items (the system prompt), and a test
    // that wants one must be able to say so.
    created_at: "created_at" in overrides ? (overrides.created_at ?? null) : "2026-05-15T12:00:00Z",
    inbound_id: overrides.inbound_id ?? null,
    show_timestamp: overrides.show_timestamp ?? true,
    partial: overrides.partial,
    interrupted: overrides.interrupted,
    reasoning_ms: overrides.reasoning_ms,
    reasoning_tokens: overrides.reasoning_tokens,
    reasoningStartedAt: overrides.reasoningStartedAt,
    reasoningElapsedMs: overrides.reasoningElapsedMs,
    codeStartedAt: overrides.codeStartedAt,
    codeElapsedMs: overrides.codeElapsedMs,
    code_elapsed_ms: overrides.code_elapsed_ms,
    execStartedAt: overrides.execStartedAt,
    exec_ms: overrides.exec_ms,
    images: overrides.images ?? null,
    image_captions: overrides.image_captions ?? null,
  };
}

describe("TimelineView accessibility", () => {
  it("is a polite log and announces turn boundaries without streaming chunks", () => {
    const { rerender } = render(<TimelineView items={[]} />);
    const log = screen.getByRole("log");
    expect(log.getAttribute("aria-live")).toBe("polite");
    expect(log.getAttribute("aria-relevant")).toBe("additions");
    expect(screen.queryByTestId("timeline-turn-announcement")).toBeNull();

    rerender(<TimelineView items={[]} turnActive />);
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent is responding.");

    rerender(
      <TimelineView
        turnActive
        items={[makeItem({ item_id: "streaming", kind: "agent_chat", payload: "A streaming chunk" })]}
      />,
    );
    expect(log.querySelector('[data-item-id="streaming"]')?.getAttribute("aria-live")).toBe("off");
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent is responding.");

    rerender(<TimelineView items={[]} />);
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent response complete.");
  });
});

describe("compact history segment dividers", () => {
  it("renders one localized divider between each compact summary and its raw history", () => {
    const items = [
      makeItem({
        item_id: "s2.older-boundary.0.0",
        kind: "system_marker",
        source: "memory",
        payload: "standing context",
      }),
      makeItem({
        item_id: "s2.older-boundary.1.0",
        kind: "inbound_compact_summary",
        payload: "older summary",
      }),
      makeItem({ item_id: "s2.older-boundary.2.0", kind: "agent_chat", payload: "older" }),
      makeItem({
        item_id: "s1.newer-boundary.0.0",
        kind: "inbound_compact_summary",
        payload: "newer summary",
      }),
      makeItem({ item_id: "s1.newer-boundary.1.0", kind: "agent_chat", payload: "newer" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "current" }),
    ];

    const { container } = render(<TimelineView items={items} />);

    const dividers = screen.getAllByTestId("compact-history-divider");
    expect(dividers).toHaveLength(2);
    expect(dividers.map((divider) => divider.dataset.segmentRank)).toEqual(["2", "1"]);
    expect(dividers.map((divider) => divider.getAttribute("aria-live"))).toEqual(["off", "off"]);
    expect(screen.getAllByText("Original history before compact")).toHaveLength(2);

    const timelineColumn = container.querySelector("[data-slot='scroll-area-viewport'] > div");
    expect(timelineColumn).not.toBeNull();
    const renderedOrder = Array.from(timelineColumn?.children ?? [])
      .filter(
        (node) =>
          node.hasAttribute("data-item-id") ||
          node.getAttribute("data-testid") === "compact-history-divider",
      )
      .map((node) =>
        node.getAttribute("data-testid") === "compact-history-divider"
          ? `divider:${node.getAttribute("data-segment-rank")}`
          : node.getAttribute("data-item-id"),
      );
    expect(renderedOrder).toEqual([
      "s2.older-boundary.0.0",
      "s2.older-boundary.1.0",
      "divider:2",
      "s2.older-boundary.2.0",
      "s1.newer-boundary.0.0",
      "divider:1",
      "s1.newer-boundary.1.0",
      "1.0",
    ]);
  });

  it("renders the divider before raw history when the oldest segment has no summary", () => {
    const items = [
      makeItem({ item_id: "s1.old-boundary.0.0", kind: "agent_chat", payload: "old" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "current" }),
    ];

    const { container } = render(<TimelineView items={items} />);
    const timelineColumn = container.querySelector("[data-slot='scroll-area-viewport'] > div");
    const renderedOrder = Array.from(timelineColumn?.children ?? [])
      .filter(
        (node) =>
          node.hasAttribute("data-item-id") ||
          node.getAttribute("data-testid") === "compact-history-divider",
      )
      .map((node) =>
        node.getAttribute("data-testid") === "compact-history-divider"
          ? "divider"
          : node.getAttribute("data-item-id"),
      );

    expect(renderedOrder).toEqual(["divider", "s1.old-boundary.0.0", "1.0"]);
  });
});

// Inter-agent / system inbound / compaction / framework-note markers default
// collapsed now (system + agent chatter folds behind a one-line header). Their
// body renders only once the header is clicked, so reveal every collapsed card
// before a per-kind body assertion. The header label stays visible either way.
// Selects via data-testid/data-expanded (the CardHeader toggle carries no
// title attribute — no hover tooltip), filtered to currently-collapsed
// buttons only: an already-expanded card must not be toggled shut here.
function revealCollapsedCards() {
  for (const btn of screen.queryAllByTestId("card-toggle")) {
    if (btn.getAttribute("data-expanded") === "false") fireEvent.click(btn);
  }
}

describe("ItemView: agent_chat", () => {
  it("partial=false → does not show 'streaming…' marker", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "hello" })]} />,
    );
    expect(screen.queryByText(/streaming…/)).toBeNull();
    expect(screen.queryByText(/streaming interrupted/i)).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hello");
  });

  it("partial=true → shows 'streaming…' label + ellipsis prefix", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "in progress", partial: true })]}
      />,
    );
    expect(screen.getByText(/streaming/)).toBeTruthy();
  });

  it("interrupted=true → shows 'streaming interrupted' label + InterruptedNotice", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "broke",
            partial: true,
            interrupted: true,
          }),
        ]}
      />,
    );
    // Both the "(streaming interrupted)" label and InterruptedNotice "Streaming interrupted…" appear
    expect(screen.getAllByText(/streaming interrupted/i).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/reconnects resume receiving/)).toBeTruthy();
  });
});

describe("ItemView: agent_code + agent_reasoning", () => {
  it("agent_code renders PythonCode below an always-visible toggle chip", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByTestId("python-code").textContent).toBe("print(1)");
    // no ava.* calls → chip falls back to the line count
    expect(screen.getByText(/1 line of code/)).toBeTruthy();
  });

  it("agent_code streamingCode=true on last item → PythonCode streaming=1", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "x" })]}
        streamingCode
      />,
    );
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });

  it("agent_reasoning renders markdown by default below a 'Thinking' toggle chip", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "thinking..." })]}
      />,
    );
    expect(screen.getByTestId("chat-markdown").textContent).toBe("thinking...");
    expect(screen.getByText("Thinking")).toBeTruthy();
  });

  it("agent_reasoning renders raw text when markdown rendering is disabled", () => {
    setUserSettings({ "display.render_reasoning_markdown": false });
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "raw thinking" })]}
      />,
    );
    expect(screen.queryByTestId("chat-markdown")).toBeNull();
    expect(screen.getByText("raw thinking").tagName).toBe("PRE");
  });
});

describe("ItemView: system_prompt", () => {
  const PROMPT = "You are Ava.\nAct via execute_code.\nThird line.";

  it("collapsed by default in 'none' mode: turn wraps system prompt, body is hidden", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    // Turn header shows the summary ("system prompt")
    expect(screen.getByText("system prompt")).toBeTruthy();
    // The prompt body is hidden (turn is collapsed in none mode)
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
    // The card's chip summary ("· 3 lines") is inside the collapsed turn — not in DOM
    expect(screen.queryByText("· 3 lines")).toBeNull();
  });

  it("clicking the turn toggle expands the turn AND the card body in one click", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    // Bug regression (#659): a detail block must expand ALL its inner content
    // together — no per-category second click.
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByText("· 3 lines")).toBeTruthy();
    expect(screen.getByText(/Act via execute_code/)).toBeTruthy();
    // Per-card collapse is still available: clicking the inner card header
    // pins that one card closed.
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
  });

  it("singular 'line' for a one-line prompt", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: "just one line", created_at: null })]}
      />,
    );
    expect(screen.getByText("· 1 line")).toBeTruthy();
  });
});

// Every Thinking / Code / Output item carries an always-visible chip header
// with the glanceable summary; the summary shows whether the content below is
// expanded (default) or collapsed.
describe("toggle chip: summary on expanded items", () => {
  it("committed thinking chip shows 'Thought for' + tokens exactly once", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    // getByText throws on multiple matches — also pins down that the duration
    // is no longer duplicated inside the expanded content.
    expect(screen.getAllByText(/Thought for 8s/)).toHaveLength(2);
    expect(screen.getByText(/1\.2k tokens/)).toBeTruthy();
    expect(screen.getByText("ponder")).toBeTruthy();
  });

  it("streaming thinking chip shows live 'Thinking for'", () => {
    // Real wire shape: a start-created streaming item carries the frontend
    // clock anchor but NO partial flag (partial marks only the
    // delta-before-start bootstrap path). The live clock must tick anyway.
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoningStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Thinking for/)).toBeTruthy();
  });

  it("frozen thinking chip (reasoningElapsedMs, no reasoning_ms yet) shows 'Thought for'", () => {
    // After the live clock stops (a later block started) but before the
    // snapshot commits the backend reasoning_ms, the chip shows the frozen
    // frontend elapsed instead of reverting to a bare "Thinking".
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoningElapsedMs: 4000,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText(/Thought for 4s/)).toHaveLength(2);
    expect(screen.queryByText(/Thinking for/)).toBeNull();
  });

  it("interrupted streaming thinking does not tick", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            partial: true,
            interrupted: true,
            reasoningStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Thinking for/)).toBeNull();
  });

  it("thinking with no timing data falls back to a plain 'Thinking' label", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_reasoning", payload: "ponder" })]} />,
    );
    expect(screen.queryByText(/Thought for|Thinking for|tokens/)).toBeNull();
    expect(screen.getByText("Thinking")).toBeTruthy();
  });

  it("agent_code chip shows per-method SDK histogram", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_code",
            payload: "ava.files.read('a')\nava.files.read('b')\nava.shell.run()",
          }),
        ]}
      />,
    );
    expect(screen.getByText("files.read")).toBeTruthy();
    expect(screen.getByText("shell.run")).toBeTruthy();
    expect(screen.getByText(/× 2/)).toBeTruthy();
  });

  it("plain-python code chip shows no histogram", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_code", payload: "x = 1\nprint(x)" })]} />,
    );
    expect(screen.queryByText(/×/)).toBeNull();
  });

  it("code_output chip shows 'ran in Xs' exactly once when exec_ms present", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "Code execution output [t]:\n\nstdout: hi",
            exec_ms: 1300,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Ran in 1s/)).toBeTruthy();
  });

  it("code_output without exec_ms shows no 'ran in' meta", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "code_output", payload: "stdout: hi" })]}
      />,
    );
    expect(screen.queryByText(/Ran in/)).toBeNull();
  });

  it("streaming code_output chip shows live 'Running for' when execStartedAt is set", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "stdout: hi",
            execStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Running for/)).toBeTruthy();
  });

  it("code_output with execStartedAt but interrupted shows no live clock", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "stdout: hi",
            execStartedAt: Date.now() - 3000,
            interrupted: true,
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Running for/)).toBeNull();
  });
});

// inbound_chat colors border / bg by source:
// - "agent:N" → violet (from another agent)
// - "user"    → gray   (sent by the user from the frontend)
// - other (e.g. "kernel" / null) → sky (system-injected)
// Test point: border color className truly tracks source.
describe("ItemView: inbound_chat source-driven color", () => {
  function getBorderEl(payloadText: string) {
    // Inter-agent / system inbound default collapsed now; reveal the body so it
    // can be located (the border container is the same either way).
    revealCollapsedCards();
    const txt = screen.getByText(payloadText);
    // Find the nearest border-l-2 ancestor div (EnvelopeBlock container)
    let cur: HTMLElement | null = txt.parentElement;
    while (cur && !cur.className.includes("border-l-2")) cur = cur.parentElement;
    if (!cur) throw new Error("no border-l-2 ancestor");
    return cur;
  }

  it("source='agent:5' → violet border (from another agent)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "agent:5",
            payload: "from agent 5",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from agent 5").className).toContain("border-violet");
  });

  it("source='user' → gray border (frontend user)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "from web",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from web").className).toContain("border-gray");
  });

  it("source='kernel' → sky border (system-injected)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "kernel",
            payload: "from kernel",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from kernel").className).toContain("border-sky");
  });

  it("source=null → sky border (default branch)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "inbound_chat", source: null, payload: "no source" }),
        ]}
      />,
    );
    expect(getBorderEl("no source").className).toContain("border-sky");
  });
});

describe("ItemView: inbound_chat envelope split", () => {
  it("payload contains '<header>\\n\\n<body>' → header and body render separately", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User [2026-05-15]:\n\nhello world",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/User/)).toBeTruthy();
    expect(screen.getByText("hello world")).toBeTruthy();
  });

  it("payload without '\\n\\n' → entire payload is body, no header", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "inbound_chat", source: "system", payload: "no envelope" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("no envelope")).toBeTruthy();
  });
});

describe("ItemView: code_output", () => {
  it("envelope split — header + body", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "Code execution output [2026-05-15]:\n\nstdout: hi",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Code execution output/)).toBeTruthy();
    expect(screen.getByText(/stdout: hi/)).toBeTruthy();
  });
});

describe("ItemView: system_marker → LifecycleChip", () => {
  it("source='lifecycle_terminate' → Terminated chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_terminate",
            payload: "[ts] You are terminated by user",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Terminated")).toBeTruthy();
    expect(screen.getByText(/terminated by user/)).toBeTruthy();
  });

  it("source='lifecycle_restart' → Restarted chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_restart",
            payload: "[ts] You have been restarted",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Restarted")).toBeTruthy();
  });

  it("source='lifecycle_resurrect' → Resurrected chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_resurrect",
            payload: "[ts] resurrected",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Resurrected")).toBeTruthy();
  });

  it("source='lifecycle_fork' → Forked chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_fork",
            payload: "[ts] forked from agent:7",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Forked")).toBeTruthy();
  });

  it("source='lifecycle_unknown' → UnknownMarkerChip red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_future",
            payload: "x",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

describe("ItemView: system_marker → ephemeral by payload", () => {
  it("payload='exec_start' → unrecognized (no longer a handled marker; falls to alarm)", () => {
    // exec_start is no longer a system_marker — the frontend now creates a
    // code_output placeholder directly. A stale marker from an old session
    // renders as UnknownMarkerChip (fail-loud red alarm).
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "exec_start" })]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });

  it("payload='compact_done' → no visible marker (internal event)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "compact_done" })]}
      />,
    );
    expect(screen.queryByText(/compact complete/)).toBeNull();
  });

  it("payload='cancelled' → silent, not rendered (stop button has no UI noise)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "cancelled" })]}
      />,
    );
    expect(screen.queryByText(/cancelled/)).toBeNull();
  });

  it("payload='compact_request:<content>' → no visible marker (internal event)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "compact_request:context too long" }),
        ]}
      />,
    );
    expect(screen.queryByText(/compact request/)).toBeNull();
  });

  it("payload='error:<content>' → '[error] <content>' (text-destructive)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "error:redis disconnected" }),
        ]}
      />,
    );
    expect(screen.getByText(/error.*redis disconnected/)).toBeTruthy();
  });

  it("unrecognized payload goes to UnknownMarkerChip red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "future_kind_not_adapted" }),
        ]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

describe("ItemView: system_marker → system_note chip", () => {
  it("source='sdk_hint' → neutral Note chip, not the red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "sdk_hint",
            payload: "[system] use ava.agents.send_message",
          }),
        ]}
      />,
    );
    // Neutral note chip renders the payload + a "Note" label, and does NOT
    // hit the UnknownMarkerChip red-alarm path every guidance note would
    // otherwise land in.
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(
      screen.getByText(/use ava\.agents\.send_message/),
    ).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='memory' → dedicated Memory chip (distinct from a guidance note)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "memory",
            payload: "[system] your standing memory",
          }),
        ]}
      />,
    );
    // memory renders its own labeled chip, not the neutral "Note" guidance chip.
    revealCollapsedCards();
    expect(screen.getByText("Memory")).toBeTruthy();
    expect(screen.queryByText("Note")).toBeNull();
    expect(screen.getByText(/your standing memory/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='compact_reminder' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "compact_reminder",
            payload: "[system] wind down soon",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='security' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "security",
            payload: "[security note] sensitive content detected",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/sensitive content detected/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='context' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "context",
            payload: "[context note] additional context injected",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/additional context injected/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='project_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "project_skills",
            payload: "[project_skills note] project skills listed here",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/project skills listed here/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='new_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "new_skills",
            payload: "[system] Skills installed since your index was built: web-ai",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/Skills installed since your index was built/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("source='preloaded_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "preloaded_skills",
            payload: "[system] Preloaded skills — full body of ultra_speed here",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/full body of ultra_speed/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
});

describe("ItemView: inbound_compact_*", () => {
  // Task #1017 regression lock: compact items must render as their Compact
  // envelope, never as the red UNRECOGNIZED SYSTEM_MARKER alarm. The alarm is
  // what a compact summary message WITHOUT an ava_msg_type stamp produced
  // (the backend catch-all classified it as system_marker source=null) —
  // both compact paths now stamp their summary messages identically.
  function expectNoUnrecognizedAlarm() {
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  }

  it("inbound_compact_summary renders envelope", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_compact_summary",
            payload: "Compact summary [ts]:\n\nbody text",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    // Card header title is now "Compact summary", and the envelope body also
    // contains "Compact summary" in its header — both match the same text.
    const matches = screen.getAllByText(/Compact summary/);
    expect(matches.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("body text")).toBeTruthy();
    expectNoUnrecognizedAlarm();
  });

  it("inbound_compact_request renders envelope", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_compact_request",
            payload: "Compact request [ts]:\n\nplease compact",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("please compact")).toBeTruthy();
    expectNoUnrecognizedAlarm();
  });
});

describe("ForkButton + CopyButton", () => {
  it("under the last agent_chat shows fork button (icon-only, aria-label)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Fork a new agent from here")).toBeTruthy();
  });

  it("forkPending=true → disabled + pulse animation", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={vi.fn()}
        forkPending
      />,
    );
    const btn = screen.getByLabelText("Fork a new agent from here").closest("button");
    if (!btn) throw new Error("btn null");
    expect(btn.hasAttribute("disabled")).toBe(true);
    expect(btn.className).toContain("animate-pulse");
  });

  it("no onFork prop → no fork button", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />,
    );
    expect(screen.queryByLabelText("Fork a new agent from here")).toBeNull();
  });

  it("click fork button → onFork called", () => {
    const onFork = vi.fn();
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={onFork}
      />,
    );
    fireEvent.click(screen.getByLabelText("Fork a new agent from here"));
    expect(onFork).toHaveBeenCalledTimes(1);
  });

  // Fork/copy's own title-tooltip removal + aria-label coverage lives in
  // "copy/fork buttons carry no title tooltip (aria-label only)" below —
  // that button relocation (below-card row → hover-reveal pill) landed
  // concurrently with this PR's tooltip sweep and already covers it.

  it("partial agent_chat does not show CopyButton (only final is copyable)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "agent_chat", payload: "streaming...", partial: true }),
        ]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });

  it("final agent_chat shows CopyButton (icon-only, aria-label)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "done" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });
});

describe("Scroll-to-bottom button + multi-item render order", () => {
  it("multi-item renders in items order (DOM order = items order)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_chat", payload: "second" }),
          makeItem({ item_id: "3", kind: "agent_chat", payload: "third" }),
        ]}
      />,
    );
    const markdowns = screen.getAllByTestId("chat-markdown");
    expect(markdowns.map((m) => m.textContent)).toEqual(["first", "second", "third"]);
  });

  it("ScrollArea wraps the content", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(screen.getByTestId("scroll-area")).toBeTruthy();
  });

  // Item 6: the timeline viewport must disable Chrome scroll anchoring so the
  // browser's autonomous scrollTop adjustments can't false-unstick the
  // sticky-bottom controller. Behavior is browser-level (jsdom can't render
  // scroll anchoring), so we only pin that the CSS is applied to the viewport.
  it("timeline viewport disables scroll anchoring (overflow-anchor: none)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByTestId("scroll-viewport").className).toContain("overflow-anchor");
  });

  // Overscroll bounce (change 2): the viewport contains its own vertical overscroll so
  // a scroll gesture that exhausts the timeline's range does not chain into a
  // page-level bounce. html/body's overscroll-behavior: none (globals.css) is
  // not exercised by jsdom/vitest — CSS-only, not asserted here.
  it("timeline viewport contains overscroll (does not chain to the page)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByTestId("scroll-viewport").className).toContain("overscroll-y-contain");
  });

  it("Scroll-to-bottom arrow button always in DOM (visibility controlled by opacity)", () => {
    render(<TimelineView items={[]} />);
    expect(screen.getByLabelText("Scroll to bottom")).toBeTruthy();
  });
});

// Item 7: cold-load spinner. A thread with no cache + no live bucket is fetching
// its first snapshot — show a spinner instead of a blank pane, and NEVER over
// content that is already on screen (a warm switch or background refetch).
describe("cold-load spinner", () => {
  it("loading + no items → shows the loading spinner", () => {
    render(<TimelineView items={[]} loading />);
    expect(screen.getByText(/Loading conversation/)).toBeTruthy();
  });

  it("loading + items present → no spinner (keep the content on screen)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "hi" })]} loading />);
    expect(screen.queryByText(/Loading conversation/)).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hi");
  });

  it("empty + not loading → no spinner (steady-state empty thread)", () => {
    render(<TimelineView items={[]} />);
    expect(screen.queryByText(/Loading conversation/)).toBeNull();
  });
});

// Scroll-up history load: the pinned top overlay (not the cold-load spinner
// above) that shows while a load-older fetch for the PREVIOUS window is in
// flight. It is always in the DOM (an absolutely-positioned overlay over the
// scroll area, not inside its scrolled content) — visibility is opacity-only,
// so it never itself shifts scrollHeight or disturbs the prepend anchor.
// jsdom does not render scroll, so the actual scroll-anchoring is not
// exercised here — see the run-collapse describe block below for the DOM-level
// piece the anchor lookup depends on (data-turn-member-ids).
describe("load-older spinner (pinned top overlay)", () => {
  it("loadingOlder=false → spinner present but hidden (opacity-0) and hidden from assistive tech", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expect(overlay?.className).toContain("opacity-0");
    expect(overlay?.className).not.toContain("opacity-100");
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
  });

  it("loadingOlder=true → spinner visible (opacity-100), stays mounted at the top, exposed to assistive tech", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expect(overlay?.className).toContain("opacity-100");
    expect(overlay?.className).not.toContain("opacity-0");
    expect(overlay?.getAttribute("aria-hidden")).toBe("false");
  });

  // The overlay sits directly over the scroll viewport at the exact spot the
  // user is scrolling through to reach older history — it must never capture
  // the gesture that triggered it (a real interaction regression a prior
  // version of this fix had: pointer-events-none was dropped while loading).
  it("pointer-events-none in BOTH states — the overlay never captures the scroll gesture that triggered it", () => {
    const { rerender } = render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(
      screen.getByText(/Loading earlier messages/).closest("div")?.className,
    ).toContain("pointer-events-none");
    rerender(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    expect(
      screen.getByText(/Loading earlier messages/).closest("div")?.className,
    ).toContain("pointer-events-none");
  });

  it("re-render with loadingOlder flipping true→false does not remount the overlay (same node, opacity only)", () => {
    const { rerender } = render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    const nodeWhileLoading = screen.getByText(/Loading earlier messages/).closest("div");
    rerender(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
        loadingOlder={false}
      />,
    );
    const nodeAfterLoad = screen.getByText(/Loading earlier messages/).closest("div");
    expect(nodeAfterLoad).toBe(nodeWhileLoading);
    expect(nodeAfterLoad?.className).toContain("opacity-0");

  });
});

// ---------------------------------------------------------------------------
// Load-older prepend anchor — bug regression (#659 + #817): the system prompt
// "0.0" is permanently attached at the front of every window (#1214), so the
// old "prepend landed" signal (items[0].item_id changed) never fired: items[0]
// stayed "0.0" across the prepend, the anchor compensation never ran, and the
// viewport was left at the top of the newly prepended window. The landing
// signal is now the first REAL item id (frontId).
//
// #817 (user report, still jumping): the ANCHOR NODE must also skip "0.0".
// 0.0's item id (0,0) sorts before every real message, so a prepend inserts
// the older window AFTER it — the 0.0 node (standalone or as a collapsed
// turn's header) is never pushed down by a landing, and anchoring to it
// yields delta ≈ 0 → no compensation → the viewport is left at the new
// window's top ("jump to the top"). The anchor is the first node whose id is
// a real message (not 0.0, not an ephemeral _marker) — the node the prepend
// actually displaces.
//
// happy-dom does not lay out, so rects are mocked: the real-content anchor
// node's top is 0 while loading and 250 after the prepend lands, and the
// compensation must add exactly that 250 to scrollTop. The 0.0 node's rect
// stays PUT (real-layout behavior) — the test proves the anchor skips it.
// ---------------------------------------------------------------------------
describe("load-older prepend anchor (#659)", () => {
  const rect = (top: number): DOMRect =>
    ({ top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("anchors to the first REAL content node, not the system prompt (0.0) — which a prepend never displaces (#817)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    // 0.0 fronts the array (id 0,0 sorts first), so a prepend inserts the
    // older window AFTER it — its rect NEVER moves (the real layout; the old
    // test mocked it as moved, which real browsers never do). The first real
    // content node (10.0) is what the landing displaces: top 0 during the
    // fetch, 250 after.
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    // Scroll near the top → load-older trigger captures the anchor.
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // A streaming commit (tail growth) must NOT consume the anchor or move
    // the viewport — the front real item is still 10.0.
    rerender(
      <TimelineView
        items={[...items, makeItem({ item_id: "12.0", kind: "agent_chat", payload: "twelve" })]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(100);

    // The older window lands: front real id 10.0 → 5.0, anchor moved down
    // 250px. The viewport must compensate — no jump to the top.
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(1),
          makeItem({ item_id: "12.0", kind: "agent_chat", payload: "twelve" }),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(350);
  });

  it("skips re-attached standing head notes — anchor and front signal land on the first real item", () => {
    // The gateway re-attaches the standing head notes (exec timeout / timezone
    // / cluster memory / agent id / agent memory) right after the prompt. They
    // are standing context like 0.0: a prepend of a same-segment window inserts
    // after them, so they must never be the scroll anchor, and the "prepend
    // landed" front signal must look past them — otherwise the first scroll-up
    // with head notes present would neither anchor nor detect the landing and
    // the viewport would be left at the top of the prepended window (#659).
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "1.0", kind: "system_marker", source: "exec_timeout", payload: "t" }),
      makeItem({ item_id: "2.0", kind: "system_marker", source: "memory", payload: "m" }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // The older window lands below the standing head notes; the real item
    // 10.0 is displaced by 250px. If 1.0 were the anchor or the front signal,
    // the compensation would no-op (its rect never moves) and the viewport
    // would stay at 100.
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          ...items.slice(1, 3),
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(3),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(350);
  });

  it("uses four-part historical ids for the prepend landing signal and exact anchor lookup", () => {
    const loadOlder = vi.fn();
    const recentHistoricalId = "s1.newer-boundary.1.0";
    const items = [
      makeItem({
        item_id: "s1.newer-boundary.0.0",
        kind: "inbound_compact_summary",
        payload: "summary",
      }),
      makeItem({ item_id: recentHistoricalId, kind: "agent_chat", payload: "recent history" }),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "current" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === recentHistoricalId) return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    moved = true;
    rerender(
      <TimelineView
        items={[
          makeItem({ item_id: "s2.older-boundary.1.0", kind: "agent_chat", payload: "older" }),
          ...items,
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );

    expect(viewport.scrollTop).toBe(350);
  });

  it.each([
    { detailsMode: "none" as const, summaryShape: "collapsed", summaryMounted: false },
    { detailsMode: "all" as const, summaryShape: "expanded", summaryMounted: true },
  ])(
    "skips pinned compact-summary context when its turn is $summaryShape",
    ({ detailsMode, summaryMounted }) => {
      setToggleState({ detailsMode });
      const loadOlder = vi.fn();
      const items = [
        makeItem({
          item_id: "0.0",
          kind: "system_prompt",
          payload: "prompt",
          created_at: null,
        }),
        makeItem({
          item_id: "6.0",
          kind: "inbound_compact_summary",
          payload: "summary",
        }),
        makeItem({ item_id: "960.1", kind: "agent_chat", payload: "recent" }),
      ];
      let moved = false;
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
        function (this: HTMLElement) {
          if (this.dataset.itemId === "960.1") return rect(moved ? 250 : 0);
          return rect(0);
        },
      );

      const { rerender } = render(
        <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
      );
      const viewport = screen.getByTestId("scroll-viewport");
      const summaryNode = viewport.querySelector('[data-item-id="6.0"]');
      expect(summaryNode == null).toBe(!summaryMounted);

      viewport.scrollTop = 100;
      viewport.dispatchEvent(new Event("scroll"));
      expect(loadOlder).toHaveBeenCalledTimes(1);

      moved = true;
      rerender(
        <TimelineView
          items={[
            items[0],
            items[1],
            makeItem({ item_id: "915.1", kind: "agent_chat", payload: "older" }),
            items[2],
          ]}
          hasMoreOlder
          onLoadOlder={loadOlder}
        />,
      );
      expect(viewport.scrollTop).toBe(350);
    },
  );

  it("skips ephemeral _marker rows too — the anchor is the first real message (#817)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "_marker.1", kind: "system_marker", payload: "m", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        // 0.0 and the ephemeral marker never move; the first real message
        // (10.0) is displaced by the landing.
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          items[1],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(2),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(350);
  });

  it("still compensates when the thread has no system prompt (front item is real)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    moved = true;
    rerender(
      <TimelineView
        items={[
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items,
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(350);
  });

  it("abandons the anchor when the content above did not grow (delta <= 0 — compact/reset, not a landing)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    // The anchor node (the first real content node, 10.0) moved UP by 50 —
    // content above it shrank/replaced (compact reset), so the viewport must
    // NOT be scrolled.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(-50);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // Items replaced wholesale (front real id 10.0 → 2.0) with the anchor
    // moved up — no scroll compensation.
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "two" }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "three" }),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(100);
  });
});

// ---------------------------------------------------------------------------
// #1272 — load-older anchor: reading-position preservation. The #659 fix left
// two failure modes open (both user-visible as "the whole list jumps after
// loading older messages"):
//   (1) the anchor was the first REAL item even when it was NOT VISIBLE. The
//       expanded 0.0 system-prompt card is tens of thousands of px tall, so a
//       user at the top of the list is reading INSIDE the card, far above the
//       first real item. The landing compensation then scrolled the viewport
//       by the anchor's whole displacement (~60k px in the field report) —
//       the reading position ended up tens of thousands of px off-screen.
//   (2) the compensation pinned the anchor to its trigger-time VIEWPORT top,
//       so scrolling during the fetch (the natural way to keep reading while
//       history loads) made every landing yank the viewport back by the
//       distance scrolled in between.
//   Fix: the anchor is the first VISIBLE real item, else the 0.0 prompt node
//   (which a prepend never displaces → the correct compensation is zero);
//   the landing scrolls by the anchor's DOCUMENT-space displacement since the
//   last commit (rect.top + scrollTop — invariant under user scrolls), so the
//   reading position never moves.
// ---------------------------------------------------------------------------
describe("load-older anchor — reading-position preservation (#1272)", () => {
  const rect = (top: number): DOMRect =>
    ({ top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("anchors to the 0.0 prompt node when no real item is visible — the landing must NOT scroll (a prepend never displaces the prompt card)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    // The expanded prompt card fills the whole viewport and every real item
    // sits far BELOW it — the user is reading inside the card. The old code
    // anchored to 10.0 anyway and scrolled the viewport by its displacement
    // on every landing (the #1272 field report: ~60k px): the prepend DOES
    // push 10.0 down (250), so the old anchor produced a 250 px yank here.
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        // The scroll viewport itself is 752 px tall (jsdom gives everything
        // a zero rect, which would break the anchor-visibility check).
        if (this.getAttribute("data-testid") === "scroll-viewport") {
          return { top: 0, bottom: 752, left: 0, right: 0, width: 0, height: 752, x: 0, y: 0, toJSON: () => ({}) };
        }
        if (this.dataset.itemId === "0.0") return rect(0); // card top — never displaced
        if (this.dataset.itemId === "10.0") return rect(moved ? 50_250 : 50_000);
        return rect(50_000); // every real item is below the viewport
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // The older window lands BELOW the prompt card — the reading position
    // (inside the card) is untouched: no compensation, scrollTop stays put.
    // (10.0 moves down 250 — the old anchor would have scrolled by it.)
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(100);
  });

  it("preserves the reading position when the user scrolled during the fetch (document-space delta, not the trigger-time viewport top)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    // The anchor's DOCUMENT position is fixed at 200 (viewport top = 200 -
    // scrollTop). The user triggers the fetch at scrollTop=199 (the trigger
    // band is < 200; anchor at viewport 1) and keeps scrolling to the very
    // top while it is in flight (anchor now at viewport 200). The landing
    // pushes the anchor down 250. Correct compensation: scroll by the
    // DOCUMENT displacement (250) only — the anchor (and the reading
    // position) stays at viewport 200. The old code pinned to the
    // trigger-time viewport top (1) and scrolled 199+250, yanking the
    // reading position 199 px back.
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        // The scroll viewport itself is 752 px tall — jsdom gives everything
        // a zero rect, which would make the anchor-visibility check treat
        // every real item as off-screen.
        if (this.getAttribute("data-testid") === "scroll-viewport") {
          return { top: 0, bottom: 752, left: 0, right: 0, width: 0, height: 752, x: 0, y: 0, toJSON: () => ({}) };
        }
        const vp = document.querySelector<HTMLElement>('[data-testid="scroll-viewport"]');
        const st = vp?.scrollTop ?? 0;
        if (this.dataset.itemId === "10.0") return rect(200 - st + (moved ? 250 : 0));
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 199;
    viewport.dispatchEvent(new Event("scroll")); // trigger — capture at viewport top 1 (doc 200)
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // The fetch goes in flight (the scroll handler's loadingOlder ref guard
    // now blocks re-capture on further scrolls — the real in-flight state).
    rerender(<TimelineView items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);

    // The user keeps scrolling up while the fetch is in flight. The capture
    // is NOT re-run (loadingOlder guard) — the compensation must still land
    // on the CURRENT reading position, not the trigger-time one. This is
    // where the old code failed: it pinned to the trigger-time VIEWPORT top
    // (1) and scrolled 199 + 250 = 449, yanking the reading position back.
    viewport.scrollTop = 0;
    viewport.dispatchEvent(new Event("scroll"));

    // The older window lands: front real id 10.0 → 5.0, anchor pushed down
    // 250 in document space. Compensation = 250 → scrollTop 0 + 250.
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(250);
  });
});

describe("fork only attaches to the last agent_chat (lastAgentChatIdx)", () => {
  it("two agent_chats → fork only attaches to the last (one fork button)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_chat", payload: "second" }),
        ]}
        onFork={vi.fn()}
      />,
    );
    expect(screen.getAllByLabelText("Fork a new agent from here")).toHaveLength(1);
  });

  it("timeline without agent_chat → no fork button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
        onFork={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Fork a new agent from here")).toBeNull();
  });

  it("agent_chat followed by agent_code → fork still attaches to the agent_chat (lastAgentChatIdx points to it)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "say hi" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "print(1)" }),
        ]}
        onFork={vi.fn()}
      />,
    );
    expect(screen.getAllByLabelText("Fork a new agent from here")).toHaveLength(1);
  });
});

describe("CopyButton click behavior", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    });
  });

  it("click copy → calls navigator.clipboard.writeText with chat text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      writable: true,
      configurable: true,
    });
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "to copy" })]}
      />,
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    // microtask: writeText called
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith("to copy");
  });

  it("navigator.clipboard unavailable (http LAN) → fallback uses document.execCommand('copy') + temporary textarea", async () => {
    // Simulate missing secure context: clipboard undefined
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      writable: true,
      configurable: true,
    });
    const execCommand = vi.fn().mockReturnValue(true);
    // eslint-disable-next-line @typescript-eslint/no-deprecated, @typescript-eslint/unbound-method -- save the legacy execCommand to restore after the spy; never invoked detached
    const origExec = document.execCommand;
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- spy on legacy execCommand fallback path
    document.execCommand = execCommand as typeof document.execCommand;
    try {
      render(
        <TimelineView
          items={[makeItem({ kind: "agent_chat", payload: "fallback text" })]}
        />,
      );
      fireEvent.click(screen.getByLabelText("Copy message"));
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith("copy"));
      // After copy, icon swaps to "Copied" feedback (aria-label changes)
      await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- restore original execCommand
      document.execCommand = origExec;
    }
  });
});

// Details mode ("all" / "last" / "none") controls the default expanded
// state for every block across all message kinds. Secondary items (thinking,
// code, output, system prompts) are grouped into turn blocks — the turn header
// shows an aggregate summary; individual cards are only visible when the turn
// is expanded.
describe("Details mode — collapse/expand", () => {
  it('detailsMode="none" → detail blocks collapsed, primary content visible, turn headers show summaries', () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "visible chat" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hidden thought",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    // In "none" mode, detail blocks collapse but primary content stays visible
    expect(screen.queryByText("hidden thought")).toBeNull();
    expect(screen.getByText("visible chat")).toBeTruthy();
    // Turn header is visible with summary (formatTurnTiming uses lowercase)
    expect(screen.getByText(/thought for 8s/i)).toBeTruthy();
  });

  it('settings still loading (isLoading) → detail blocks collapsed, no "all"-default flash', () => {
    // While the DB-backed setting is in flight, useContentToggle's detailsMode
    // is the USER_SETTING_DEFAULTS fallback ("all") — the timeline must NOT
    // render every block expanded from that, or every cold load (refresh /
    // app start / desktop rollout reload) flashes all detail blocks open and
    // then collapses them when the real value lands. Regression for the
    // "details level is none but blocks auto-expand" report.
    setToggleState({ detailsMode: "all", isLoading: true });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "visible chat" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hidden thought",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    expect(screen.queryByText("hidden thought")).toBeNull();
    expect(screen.getByText("visible chat")).toBeTruthy();
  });

  it("settings load completes → the real mode applies without a flash", () => {
    setToggleState({ detailsMode: "all", isLoading: true });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "chat" }),
      makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "thought" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    expect(screen.queryByText("thought")).toBeNull();
    // Settings land with mode "all" → blocks expand to the real default
    setToggleState({ detailsMode: "all", isLoading: false });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("thought")).toBeTruthy();
  });

  it('detailsMode="last" → primary items (agent_chat) always visible', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "first chat" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "last chat" }),
        ]}
      />,
    );
    // Primary items (agent_chat) are always expanded in all modes — "last"
    // only collapses secondary detail blocks (thinking / code / output).
    expect(screen.getByText("first chat")).toBeTruthy();
    expect(screen.getByText("last chat")).toBeTruthy();
  });

  it('detailsMode="last" → last item follows streaming content', () => {
    setToggleState({ detailsMode: "last" });
    // Use a primary item (agent_chat) so it's not grouped into a turn
    render(
      <TimelineView
        items={[
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            payload: "streaming message",
          }),
        ]}
      />,
    );
    expect(screen.getByText("streaming message")).toBeTruthy();
  });

  it('detailsMode="all" → all blocks expanded', () => {
    setToggleState({ detailsMode: "all" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "first chat" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "second chat" }),
        ]}
      />,
    );
    // Both primary items visible in "all" mode
    expect(screen.getByText("first chat")).toBeTruthy();
    expect(screen.getByText("second chat")).toBeTruthy();
  });

  it("clicking an expanded primary card header collapses it (override for 'none')", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the visible chat" }),
        ]}
      />,
    );
    // In "none" mode, primary item (agent_chat) stays visible — not a detail block
    expect(screen.getByText("the visible chat")).toBeTruthy();
    // Click the card header to collapse (override)
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the visible chat")).toBeNull();
  });

  it("default expanded → clicking card header collapses content (override)", () => {
    setToggleState({ detailsMode: "all" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the open chat" }),
        ]}
      />,
    );
    expect(screen.getByText("the open chat")).toBeTruthy();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the open chat")).toBeNull();
  });

  it("detailsMode change clears per-item overrides", () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the chat" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    // Collapse from "all" default
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the chat")).toBeNull();
    // Switch to "none" — overrides cleared, primary items expanded per new default
    setToggleState({ detailsMode: "none" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("the chat")).toBeTruthy();
    // Switch back to "all" — overrides cleared, expanded per new default
    setToggleState({ detailsMode: "all" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("the chat")).toBeTruthy();
  });
});

// streamingCode flag should only apply to the last agent_code, not all of them.
// Verifies that the index === items.length - 1 guard hasn't regressed.
describe("streamingCode last-item only", () => {
  it("last is agent_code + streamingCode=true → last streaming=1, previous agent_code streaming=0", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_code", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "last" }),
        ]}
        streamingCode
      />,
    );
    const blocks = screen.getAllByTestId("python-code");
    expect(blocks).toHaveLength(2);
    expect(blocks[0].getAttribute("data-streaming")).toBe("0");
    expect(blocks[1].getAttribute("data-streaming")).toBe("1");
  });

  it("last is agent_chat (not agent_code) + streamingCode=true → previous agent_code not marked streaming", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_code", payload: "code" }),
          makeItem({ item_id: "2", kind: "agent_chat", payload: "follow-up chat" }),
        ]}
        streamingCode
      />,
    );
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
  });
});

// lastAgentChatIdx uses a reverse for-loop (i-- decrement). UnaryOperator
// mutants flip i-- to +i — the loop never advances. Killed by the
// "fork attached to the last of multiple agent_chats" assertion below.
describe("lastAgentChatIdx reverse scan", () => {
  it("agent_chat → agent_code → agent_chat → agent_code: fork attaches to row 3 (second agent_chat), not row 1", () => {
    const onFork = vi.fn();
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first chat" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "code1" }),
          makeItem({ item_id: "3", kind: "agent_chat", payload: "second chat" }),
          makeItem({ item_id: "4", kind: "agent_code", payload: "code2" }),
        ]}
        onFork={onFork}
      />,
    );
    // Only one fork (lastAgentChatIdx picks the last agent_chat, not the first)
    const forks = screen.getAllByLabelText("Fork a new agent from here");
    expect(forks).toHaveLength(1);
    // Fork button (icon-only, aria-label) attaches only inside the second
    // agent_chat's card. Verify it's a descendant of the card containing
    // "second chat", not "first chat".
    const forkBtn = forks[0];
    const secondChatCard = screen.getByText("second chat").closest('[class*="group/msgcard"]');
    const firstChatCard = screen.getByText("first chat").closest('[class*="group/msgcard"]');
    expect(secondChatCard).toBeTruthy();
    expect(secondChatCard!.contains(forkBtn)).toBe(true);
    expect(firstChatCard?.contains(forkBtn)).toBe(false);
  });
});

// splitEnvelope header length cap — prevents a long stdout first paragraph being mistaken for the header.
describe("splitEnvelope header length guard", () => {
  it("\\n\\n distance > 200 chars → entire payload is body, no header split", () => {
    const longLine = "x".repeat(300);
    const payload = `${longLine}\n\nbody after long line`;
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload,
          }),
        ]}
      />,
    );
    // The whole thing (long line + body) sits inside one <pre>
    const preEl = screen.getByText(/body after long line/);
    expect(preEl.textContent).toContain(longLine);
  });

  it("\\n\\n at char 0 (idx=0) → entire payload is body, no empty header", () => {
    // idx===0 fails the (idx > 0) guard; fall back to whole payload as body
    const payload = "\n\nbody with leading newlines";
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    expect(screen.getByText(/body with leading newlines/)).toBeTruthy();
  });
});

// UnknownMarkerChip goes through console.warn so devs can catch unadapted cases in DevTools.
describe("UnknownMarkerChip console.warn", () => {
  it("Unrecognized system_marker → console.warn emits source + payload", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({
              kind: "system_marker",
              source: "future_source",
              payload: "future_payload",
            }),
          ]}
        />,
      );
      expect(warnSpy).toHaveBeenCalled();
      // both payload and source should be passed into warn
      const lastCall = warnSpy.mock.calls.at(-1);
      if (!lastCall) throw new Error("no warn call");
      const callJson = JSON.stringify(lastCall);
      expect(callJson).toContain("future_source");
      expect(callJson).toContain("future_payload");
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// formatItemTime / ItemTimestamp — render a local-timezone timestamp from the iso string.
// Test points: invalid iso → no timestamp rendered; valid iso → renders `[YYYY-MM-DD HH:MM:SS TZ]` format.
describe("ItemTimestamp", () => {
  it("invalid iso → no timestamp span rendered (Number.isNaN(d.getTime()) guard)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "this is not a date",
          }),
        ]}
      />,
    );
    // No timestamp block starting with `[` should appear in the DOM
    expect(screen.queryByText(/\[\d{4}-\d{2}-\d{2}/)).toBeNull();
  });

  it("valid iso → renders [YYYY-MM-DD DayOfWeek HH:MM:SS TZ] format, no hover tooltip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "2026-05-15T12:00:00Z",
          }),
        ]}
      />,
    );
    // bracket + 4-digit year + dash + 2-digit month + 2-digit day + optional weekday + space + HH:MM:SS + space + TZ.
    // Don't pin timezone (differs CI vs local) — verify structure only.
    const stamp = screen.getByText(/\[\d{4}-\d{2}-\d{2}( [A-Z][a-z]{2})? \d{2}:\d{2}:\d{2} [A-Z]+\]/);
    expect(stamp).toBeTruthy();
    // No title attribute — no native hover tooltip on the timestamp.
    expect(stamp.getAttribute("title")).toBeNull();
  });

  it("empty iso (created_at='') → no timestamp rendered", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/\[\d{4}-\d{2}-\d{2}/)).toBeNull();
  });
});

// CopyButton 1s feedback + rapid repeat-click resets the timer.
describe("CopyButton feedback state machine", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    });
  });

  it("click copy → 'copy' flips immediately to 'copied'", async () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
  });

  it("after 1s reverts to 'copy' automatically (timer fires)", async () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
    // wait for 1s timeout to trigger setCopied(false)
    await waitFor(() => expect(screen.getByLabelText("Copy message")).toBeTruthy(), {
      timeout: 1500,
    });
  });
});

// The icon-only CopyButton from ./copy-button (aria-label "Copy <label>", no
// visible text) is separate from the block-level copy button
// (timeline/buttons.tsx, covered by the "block-level copy/fork actions"
// describe block below). agent_code (via PythonCode) and code_output (via
// EnvelopeContent showCopy) render ONLY the icon-only per-block copy —
// the card-level block copy is skipped for these kinds to avoid duplicate
// overlapping buttons that copy the same payload. This block only guards the
// icon-only one; it must not appear under a still-partial agent_chat (no
// code/output content to speak of) or an ephemeral system_marker (no card).
describe("CopyButton render guard (icon-only, code/output-scoped)", () => {
  it("agent_code item shows copy button (via PythonCode)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy code/i })).toBeTruthy();
  });

  it("code_output item shows copy button (via EnvelopeContent showCopy)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "code_output", payload: "stdout" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy command output/i })).toBeTruthy();
  });

  it("system_marker item shows no copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "exec_start" })]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });

  it("partial agent_chat (in progress) shows no copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "streaming", partial: true })]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
});

// The general block-level copy (text "copy", timeline/buttons.tsx) attaches
// inside EVERY card's bottom-right corner via MessageCard's `actions` overlay
// — any kind that renders a card (messageCardConfig !== null), once expanded
// and no longer partial. Fork attaches additionally, only under the last
// agent_chat card. Both mount only while eligible — a collapsed card has
// neither in the DOM at all (not just hidden).
describe("block-level copy/fork actions (MessageCard overlay)", () => {
  it("human inbound_chat (a user message) shows the copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "inbound_chat", source: "user", payload: "hi" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });

  it("agent_reasoning shows the block-level copy button once expanded", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "thinking" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });

  it("agent_code has only the icon-only body copy (card-level duplicate suppressed)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy code/i })).toBeTruthy();
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });

  it("code_output has only the icon-only body copy (card-level duplicate suppressed)", () => {
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload: "stdout" })]} />,
    );
    expect(screen.getByRole("button", { name: /copy command output/i })).toBeTruthy();
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });

  it("collapsed-by-default card (system_prompt) shows no copy button until the turn is expanded", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView items={[makeItem({ kind: "system_prompt", payload: "line1\nline2" })]} />,
    );
    // Turn is collapsed → no copy button anywhere
    expect(screen.queryByLabelText("Copy message")).toBeNull();
    // Expanding the turn expands the inner card too (bug #659) — the copy
    // button appears in the same click.
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
    // Collapsing the inner card hides the copy button again.
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });

  it("action row renders fork before copy — copy is always the rightmost action", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={vi.fn()}
      />,
    );


    // Icon-only buttons: check order by DOM position, not text content
    const buttons = screen.getAllByRole("button");
    const labels = buttons.map(b => b.getAttribute("aria-label"));
    const forkIdx = labels.indexOf("Fork a new agent from here");
    const copyIdx = labels.indexOf("Copy message");
    expect(forkIdx).toBeGreaterThan(-1);
    expect(forkIdx).toBeLessThan(copyIdx);
  });

  it("copy/fork buttons carry no title tooltip (aria-label only)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={vi.fn()}
      />,
    );
    const copyBtn = screen.getByLabelText("Copy message").closest("button");
    const forkBtn = screen.getByLabelText("Fork a new agent from here").closest("button");
    expect(copyBtn?.hasAttribute("title")).toBe(false);
    expect(forkBtn?.hasAttribute("title")).toBe(false);
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
    expect(screen.getByLabelText("Fork a new agent from here")).toBeTruthy();
  });

  // Actual hover reveal is CSS-only (opacity-0 / group-hover/msgcard) — jsdom
  // has no layout/paint engine to simulate a real :hover-triggered opacity
  // change, so this only pins the structural classes are present, NOT that
  // hovering visually reveals the pill. See PR description NOT-tested note.
  it("actions pill starts hidden (opacity-0) and is wired to reveal on card hover", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />,
    );
    const pill = screen.getByLabelText("Copy message").closest("button")?.parentElement;
    expect(pill?.className).toContain("opacity-0");
    expect(pill?.className).toContain("group-hover/msgcard:opacity-100");
  });

  // A hidden pill (opacity-0) is still hit-testable unless pointer-events is
  // ALSO gated — otherwise a tap in the card's bottom-right corner on a touch
  // device (no real :hover) silently hits an unseen copy/fork button.
  it("actions pill defaults to pointer-events-none while hidden; only switches on with the same hover/focus variant that reveals it", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
        onFork={vi.fn()}
      />,
    );
    const pill = screen.getByLabelText("Copy message").closest("button")?.parentElement;
    expect(pill?.className).toContain("pointer-events-none");
    expect(pill?.className).toContain("group-hover/msgcard:pointer-events-auto");
    expect(pill?.className).toContain("focus-within:pointer-events-auto");
    // Coarse-pointer (touch) devices opt out of the hover gate entirely —
    // no real :hover there, so the pill must stay reachable without one.
    expect(pill?.className).toContain("pointer-coarse:opacity-100");
    expect(pill?.className).toContain("pointer-coarse:pointer-events-auto");
  });
});

// splitEnvelope `idx > 0 && idx < 200` boundary — confirm idx === 200 → entire payload is body (no split). idx === 199 still splits.
describe("splitEnvelope boundary", () => {
  it("idx === 200 (\\n\\n exactly at char 200) → entire payload is body, no header", () => {
    // header length 200 (exactly the bound, idx === 200) — `idx < 200` is false; entire payload falls back to body
    const header = "h".repeat(200);
    const payload = `${header}\n\nbody-200-boundary`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    // body field contains the full header + \n\n + body-200-boundary (entire payload as body)
    const bodyEl = screen.getByText(/body-200-boundary/);
    expect(bodyEl.textContent).toContain(header);
  });

  it("idx === 199 (\\n\\n exactly at char 199) → splits into header + body normally", () => {
    const header = "h".repeat(199);
    const payload = `${header}\n\nbody-199`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    // header and body should be independent — body should not contain header text
    const bodyEl = screen.getByText("body-199");
    expect(bodyEl.textContent).toBe("body-199");
  });
});

// UnknownMarkerChip renders source=null as the literal string "null",
// and a non-null source as a JSON-quoted string. Pins the
// `source === null ? "null" : JSON.stringify(source)` ternary.
describe("UnknownMarkerChip source render", () => {
  it("source=null → renders 'source = null' (no quotes)", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({ kind: "system_marker", source: null, payload: "wat" }),
          ]}
        />,
      );
      // null renders as the bare string "null", not as a quoted "null" literal
      expect(screen.getByText(/source = null$/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });

  it("source is a string → renders JSON.stringify (with double quotes)", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({
              kind: "system_marker",
              source: "future:kind",
              payload: "wat",
            }),
          ]}
        />,
      );
      // JSON.stringify("future:kind") = '"future:kind"' (with double quotes)
      expect(screen.getByText(/source = "future:kind"/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// formatItemTime: hour12: false is 24-hour format — verified by
// spying on Intl.DateTimeFormat. Stubbing the entire Intl.DateTimeFormat
// behavior is impractical, so a spy on the call args is used instead.
describe("formatItemTime: hour12=false (24-hour format)", () => {
  it("Intl.DateTimeFormat called with hour12=false (pins 24-hour format)", () => {
    const origDTF = Intl.DateTimeFormat;
    const calls: { locale: unknown; options: Intl.DateTimeFormatOptions | undefined }[] = [];
    // class wrapper preserves constructor semantics + formatToParts via the original impl
    class SpyDTF extends origDTF {
      constructor(locale?: string | string[], options?: Intl.DateTimeFormatOptions) {
        super(locale, options);
        calls.push({ locale, options });
      }
    }
    Object.defineProperty(Intl, "DateTimeFormat", {
      value: SpyDTF,
      writable: true,
      configurable: true,
    });
    try {
      render(
        <TimelineView
          items={[
            makeItem({
              kind: "agent_chat",
              payload: "x",
              created_at: "2026-05-15T12:00:00Z",
            }),
          ]}
        />,
      );
      // formatItemTime called at least once, and options.hour12 === false
      const tsCall = calls.find((c) => c.options?.timeZoneName === "short");
      if (!tsCall) throw new Error("no timeZoneName=short call found");
      expect(tsCall.options?.hour12).toBe(false);
    } finally {
      Object.defineProperty(Intl, "DateTimeFormat", {
        value: origDTF,
        writable: true,
        configurable: true,
      });
    }
  });
});

// After ContentToggle store changes, useMemo deps must include
// detailsMode so a mode flip triggers re-filtering. Without detailsMode
// in deps, frequent re-renders during streaming would skip re-filtering
// — the user toggle would do nothing.
describe("ContentToggle deps completeness (mid-mount toggle)", () => {
  it("rerender with detailsMode flip → visibleItems re-filtered", () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1", kind: "agent_chat", payload: "chat" }),
      makeItem({ item_id: "2", kind: "agent_reasoning", payload: "deep thought" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    // "all" → both items visible
    expect(screen.getByText("chat")).toBeTruthy();
    expect(screen.getByText("deep thought")).toBeTruthy();
    // flip to "none" → rerender (same items reference)
    setToggleState({ detailsMode: "none" });
    rerender(<TimelineView items={items} />);
    // In "none" mode, primary item (agent_chat) stays visible; secondary (reasoning) collapses into turn
    expect(screen.getByText("chat")).toBeTruthy();
    expect(screen.queryByText("deep thought")).toBeNull();
  });

  it('rerender with detailsMode "last" → primary items always visible, detail turn collapsed without turnActive', () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
      makeItem({ item_id: "2", kind: "agent_code", payload: "last_code()" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByTestId("python-code")).toBeTruthy();
    setToggleState({ detailsMode: "last" });
    rerender(<TimelineView items={items} />);
    // Primary items (agent_chat) always visible in all modes.
    expect(screen.getByText("first")).toBeTruthy();
    // Detail turn collapsed in "last" mode without turnActive — turn not expanded.
    expect(screen.queryByTestId("python-code")).toBeNull();
  });

  it('detailsMode="last" with turnActive → last detail turn auto-expanded', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "streaming_code()" }),
        ]}
      />,
    );
    // Primary visible
    expect(screen.getByText("first")).toBeTruthy();
    // Detail turn auto-expanded while turnActive
    expect(screen.getByTestId("python-code")).toBeTruthy();
  });
});

// ItemView memo's custom comparator (prev.item === next.item &&
// prev.streaming === next.streaming). When streamingCode flips on the
// last item (false → true), the last PythonCode streaming prop must
// update. Pins down the comparator returning false (don't reuse the
// memo cache) — a mutant that becomes `() => undefined` (falsy → always
// re-render) was a Stryker survivor; a mutant `() => true` (always
// reuse) would make streaming flips no-ops.
describe("ItemView memo comparator: streaming prop flip is visible", () => {
  it("streamingCode false → true rerender, last agent_code data-streaming should update to 1", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
    rerender(<TimelineView items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });

  it("streamingCode true → false rerender, last agent_code data-streaming should update to 0", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
    rerender(<TimelineView items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
  });
});

// system_marker payload with `error:` prefix → goes through
// EphemeralMarker isError=true, rendering destructive color.
// payload === "error:" (prefix but empty content) → label "[error] " (empty content).
describe("system_marker error prefix boundary", () => {
  it("payload='error:' (empty content) → still goes to error branch ([error] empty)", () => {
    render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "error:" })]} />,
    );
    // label is `[error] ` (followed by empty string)
    expect(screen.getByText(/error/)).toBeTruthy();
  });

  it("payload contains 'error' but no colon → skips error branch, goes to UnknownMarkerChip", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[makeItem({ kind: "system_marker", payload: "error_no_colon" })]}
        />,
      );
      expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });
});


// splitEnvelope idx === 0 boundary: when payload starts with \n\n,
// must not treat the empty string as a header and swallow the leading
// \n\n. Pins down `idx > 0` (strict greater) — mutant `idx >= 0` would
// swallow the leading \n\n.
describe("splitEnvelope idx === 0 boundary", () => {
  it("payload starts with \\n\\n → body keeps leading \\n\\n (entire payload as body), no split", () => {
    const payload = "\n\nthe-body-text";
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    // Find the <pre> body element; verify textContent retains leading \n\n
    // (in the DOM \n\n renders as two newline chars)
    const bodyEl = screen.getByText(/the-body-text/);
    // Original code: idx > 0 fails → entire payload falls back to body; textContent contains leading \n\n
    expect(bodyEl.textContent).toBe("\n\nthe-body-text");
  });
});

// Force-scroll is now driven by the store's single scrollToBottomRequest
// signal (bumped on agent switch inside switchThread and on
// send via requestScrollToBottom), consumed by one useLayoutEffect. The
// signal's bump-vs-no-bump contract is covered in store.test.ts; jsdom does
// not render scroll, so the pin itself stays covered only by sticky.test.ts.
// Here we just confirm the timeline mounts and renders content without a
// force-scroll prop.
describe("force-scroll: renders without a scroll-token prop", () => {
  it("mounts and renders content (scrollToBottomRequest drives the pin, no prop)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByText("x")).toBeTruthy();
  });
});

// EphemeralMarker isError=true → uses text-destructive (red);
// isError=false → text-muted-foreground. Pins isError default = false
// and the error branch's red-only color.
describe("EphemeralMarker isError path", () => {
  it("payload='error:msg' → renders text-destructive class (red error)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "error:failed" })]} />,
    );
    const errEl = container.querySelector(".text-destructive");
    if (!errEl) throw new Error("no destructive el");
    expect(errEl.textContent).toMatch(/error.*failed/);
  });

  it("payload='exec_start' → unrecognized, renders text-destructive (red alarm)", () => {
    // exec_start is no longer a handled marker — falls through to
    // UnknownMarkerChip which uses text-destructive red.
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "exec_start" })]} />,
    );
    const errEl = container.querySelector(".text-destructive");
    expect(errEl).toBeTruthy(); // red alarm for unrecognized marker
  });

  it("payload='compact_done' → no text-destructive rendered (renders nothing)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "compact_done" })]} />,
    );
    // compact_done is suppressed — no marker rendered at all
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(screen.queryByText(/compact/)).toBeNull();
  });
});

// Each of the three LifecycleChip kinds should fully render its
// visual label and payload. Additional payload variants here pin down
// `<div>{payload}</div>` against a mutant turning it into `<div></div>`.
describe("LifecycleChip payload render", () => {
  it("terminate chip fully renders payload string", () => {
    const payload = "[2026-05-15 12:00:00 PDT] You are terminated by user";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_terminate", payload })]}
      />,
    );
    revealCollapsedCards();
    // Full payload text must appear (cannot be truncated)
    expect(screen.getByText(payload)).toBeTruthy();
  });

  it("restart_completed chip fully renders payload string", () => {
    const payload = "[ts] You have been restarted by yourself";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_restart", payload })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(payload)).toBeTruthy();
  });

  it("resurrect chip fully renders payload string", () => {
    const payload = "[ts] You were resurrected by user";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_resurrect", payload })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(payload)).toBeTruthy();
  });
});

// ItemTimestamp className contains tabular-nums and ml-2 — monospaced
// digits + spacing from the label. No title attribute (no hover tooltip) —
// see the "ItemTimestamp" describe block above for that assertion.
describe("ItemTimestamp label content", () => {
  it("ChatMarkdown label 'Ava ▸' appears (pins label JSX against deletion)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
      />,
    );
    expect(screen.getByText(/Ava ▸/)).toBeTruthy();
  });

  it("plain agent_chat label is exactly 'Ava ▸' — no i18n key leak (Task #816)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
      />,
    );
    // Exact-match the whole label: cardTitle must pass suffix:"" so next-intl
    // compiles "Ava ▸{suffix}" → "Ava ▸". A missing interpolation variable
    // makes real next-intl fall back to the raw key ("timeline.agentChat"),
    // which leaked to users (Task #816); leftover "{suffix}" would mean the
    // value never reached the message.
    expect(screen.getByText(/^Ava ▸$/)).toBeTruthy();
    expect(screen.queryByText(/timeline\.agentChat/)).toBeNull();
    expect(screen.queryByText(/\{suffix\}/)).toBeNull();
  });

  it("agent_chat partial=true → label contains '(streaming…)'", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true })]}
      />,
    );
    expect(screen.getByText(/\(streaming…\)/)).toBeTruthy();
  });

  it("agent_chat interrupted=true → label contains '(streaming interrupted)'", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true, interrupted: true })]}
      />,
    );
    // label should prefer interrupted over partial
    const labels = screen.getAllByText(/streaming interrupted/i);
    expect(labels.length).toBeGreaterThan(0);
  });
});

describe("ItemView: inbound_chat images", () => {
  it("renders an <img> per image url on a multimodal inbound", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User:\n\nwhat is this",
            images: ["/api/agents/7/uploads/shot.png"],
          }),
        ]}
      />,
    );
    const img = screen.getByAltText("attached image");
    expect(img.getAttribute("src")).toContain("/api/agents/7/uploads/shot.png");
  });

  it("attach item renders data-URI images raw (no API_BASE prefix)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload: "[system] Files attached during this turn:\n- [1] render.png (image/png, 1.2 KiB)",
            images: ["data:image/png;base64,iVBORw0KGgo="],
          }),
        ]}
      />,
    );
    const img = screen.getByAltText("attached image");
    expect(img.getAttribute("src")).toBe("data:image/png;base64,iVBORw0KGgo=");
    // Caption body still renders beside the thumbnail.
    expect(screen.getByText(/render.png/)).toBeTruthy();
  });

  it("image-only inbound hides the [image] placeholder text but shows the thumbnail", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User:\n\n[image]",
            images: ["/api/agents/7/uploads/a.png"],
          }),
        ]}
      />,
    );
    expect(screen.getByAltText("attached image")).toBeTruthy();
    expect(screen.queryByText("[image]")).toBeNull();
  });
});

describe("ItemView: attach interleaving + lightbox (user 2026-08-27)", () => {
  const NOTICE = "[system] Files attached during this turn:";
  const LINE1 = '- [1] screen.png (image/png, 12.3 KiB) — "shot A"';
  const LINE2 = '- [2] chart.png (image/png, 45.6 KiB) — "chart B"';
  const IMG1 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB";
  const IMG2 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAAB";
  const payload = `${NOTICE}\n${LINE1}\n${LINE2}`;

  const imgs = () => screen.getAllByAltText("attached image");
  const orderOf = (el: Element) => {
    // The timeline renders the whole tree into one container; find the
    // document-order position of each node among all timeline descendants.
    const all = Array.from(document.body.querySelectorAll("*"));
    return all.indexOf(el);
  };

  it("interleaves label line → thumbnail → label line → thumbnail", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    const line1 = screen.getByText(LINE1);
    const line2 = screen.getByText(LINE2);
    const [img1, img2] = imgs();
    expect(orderOf(line1)).toBeLessThan(orderOf(img1));
    expect(orderOf(img1)).toBeLessThan(orderOf(line2));
    expect(orderOf(line2)).toBeLessThan(orderOf(img2));
    // No navigation wrapper around the thumbnails anymore.
    expect(document.body.querySelector('a[href^="data:image"]')).toBeNull();
  });

  it("renders a skipped (no-image) line in place without shifting alignment", () => {
    const skipped = "- [2] notes.pdf (application/pdf, 2.0 KiB) — not delivered: your model cannot receive pdf";
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload: `${NOTICE}\n${LINE1}\n${skipped}\n${LINE2}`,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    const line1 = screen.getByText(LINE1);
    const sk = screen.getByText(skipped);
    const line2 = screen.getByText(LINE2);
    const [img1, img2] = imgs();
    expect(orderOf(line1)).toBeLessThan(orderOf(img1));
    expect(orderOf(img1)).toBeLessThan(orderOf(sk));
    expect(orderOf(sk)).toBeLessThan(orderOf(line2));
    expect(orderOf(line2)).toBeLessThan(orderOf(img2));
  });

  it("clicking a thumbnail opens the lightbox; backdrop click and Escape close it", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    // Thumbnails declare loading="lazy" so large data-URI images do not block
    // initial paint (QA nit #831-3).
    expect(screen.getAllByTestId("attach-thumbnail")[0].querySelector("img")?.getAttribute("loading")).toBe("lazy");
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const lightbox = screen.getByTestId("attach-lightbox");
    // The enlarged image is the lightbox content.
    expect(lightbox.querySelector("img")?.getAttribute("src")).toBe(IMG1);
    expect(lightbox.querySelector("img")?.getAttribute("loading")).toBe("lazy");
    // Click anywhere on the backdrop closes.
    fireEvent.click(lightbox);
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    // Escape closes too.
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[1]);
    expect(screen.getByTestId("attach-lightbox")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
  });

  it("lightbox traps focus while open and returns it to the trigger on close", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    const [thumb0, thumb1] = screen.getAllByTestId("attach-thumbnail");
    fireEvent.click(thumb0);
    const lightbox = screen.getByTestId("attach-lightbox");
    // Focus moves into the dialog on open.
    expect(document.activeElement).toBe(lightbox);
    // Background scroll is locked while open.
    expect(document.body.style.overflow).toBe("hidden");
    // Tab stays inside the modal (trap).
    fireEvent.keyDown(lightbox, { key: "Tab" });
    expect(document.activeElement).toBe(lightbox);
    // Escape closes and focus returns to the trigger button; scroll unlocks.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    expect(document.activeElement).toBe(thumb0);
    expect(document.body.style.overflow).toBe("");
    // Re-opening focuses the dialog again (not the button).
    fireEvent.click(thumb1);
    expect(document.activeElement).toBe(screen.getByTestId("attach-lightbox"));
  });

  it("mounts the lightbox at document.body so the backdrop is not clipped by the contained timeline row", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const lightbox = screen.getByTestId("attach-lightbox");
    // Root cause (QA #1085): `.timeline-item` carries
    // `content-visibility: auto` (globals.css), whose paint containment turns
    // the row into the containing block of `fixed` descendants AND a stacking
    // context — an inline dialog was clipped to the row's box (~440x861 vs
    // the 1182x839 viewport) and its z-50 stayed under the z-30
    // scroll-to-bottom button. Portal mounting escapes both: the overlay is
    // hung off document.body, outside every row.
    expect(document.body.contains(lightbox)).toBe(true);
    expect(lightbox.closest("[data-item-id]")).toBeNull();
    // Root stacking context: z-50 at body level paints above every app-layer
    // control (header z-20, composer z-20, scroll-to-bottom z-30).
    expect(lightbox.className).toContain("fixed inset-0 z-50");
  });

  it("enlarged image is capped at 80% of the viewport (contain, no upscale)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: [LINE1, LINE2],
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const img = screen.getByTestId("attach-lightbox").querySelector("img");
    // 60-80% viewport sizing (user ruling 2026-08-30): the caps are the 80%
    // bound — a big image scales down to fit either axis, a small one keeps
    // its natural size (max-* only ever shrinks), aspect ratio preserved.
    expect(img?.className).toContain("max-h-[80vh]");
    expect(img?.className).toContain("max-w-[80vw]");
    expect(img?.className).toContain("object-contain");
  });
  it("reserves the thumbnail's max box so lazy img load never shifts layout (QA review-955)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1],
            image_captions: [LINE1],
          }),
        ]}
      />,
    );
    const thumb = screen.getByTestId("attach-thumbnail");
    // The button is the fixed-size layout node — its box equals the img caps
    // (max-w-[16rem] × max-h-48), so the row height is stable from first paint.
    expect(thumb.className).toContain("relative");
    expect(thumb.className).toContain("h-48");
    expect(thumb.className).toContain("w-64");
    // The img is absolutely positioned and centered: its own box (2x2px before
    // decode -> capped content size after) is out of flow, so growth cannot move
    // the content below — zero CLS on the scroll path.
    const img = thumb.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!.className).toContain("absolute");
    expect(img!.className).toContain("inset-0");
    expect(img!.className).toContain("m-auto");
    expect(img!.className).toContain("max-h-48");
    expect(img!.className).toContain("max-w-[16rem]");
    expect(img!.className).toContain("object-contain");
  });

  it("legacy attach (images without image_captions) still renders thumbnails without navigation", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload,
            images: [IMG1, IMG2],
            image_captions: null,
          }),
        ]}
      />,
    );
    expect(imgs()).toHaveLength(2);
    expect(document.body.querySelector('a[href^="data:image"]')).toBeNull();
  });
});

describe("turn-collapse (always on — Turns toggle controls expand/collapse)", () => {

  const runToggle = () => screen.getByTestId("turn-toggle");

  it("drops a pinned turn expansion when the timeline thread changes", () => {
    setToggleState({ detailsMode: "none" });
    const threadAItems = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A code" }),
    ];
    const { rerender } = render(
      <TimelineView threadKey="1" items={threadAItems} />,
    );

    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(runToggle());
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread A code");

    // A normal refresh of the same thread preserves the user's pin.
    rerender(
      <TimelineView
        threadKey="1"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A update" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A updated code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread A updated code");

    // Item ids restart in each thread; the matching id must not resurrect A's pin in B.
    rerender(
      <TimelineView
        threadKey="2"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread B thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread B code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();
  });

  it("drops a pinned inner-card collapse when the timeline thread changes", () => {
    setToggleState({ detailsMode: "all" });
    const { rerender } = render(
      <TimelineView
        threadKey="1"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A code" }),
        ]}
      />,
    );

    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread A code");
    fireEvent.click(screen.getAllByTestId("card-toggle")[1]);
    expect(screen.queryByTestId("python-code")).toBeNull();

    rerender(
      <TimelineView
        threadKey="2"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread B thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread B code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread B code");
  });

  it("folds a run of ≥2 secondary items into an expanded turn block by default", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "0.0", kind: "agent_chat", payload: "hi" }),
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
          makeItem({ item_id: "1.2", kind: "code_output", payload: "out" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    // The aggregate summary shows action counts (thinking/code/output).
    const runHeader = runToggle();
    expect(runHeader.textContent).toContain("thinking");
    expect(runHeader.textContent).toContain("code");


    // Default expanded (detailsMode='all').
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(runHeader.getAttribute("title")).toBeNull();
    // Human-readable backbone (the two agent replies) stays visible …
    expect(screen.getAllByTestId("chat-markdown").map((m) => m.textContent)).toEqual([
      "hi",
      "thinking",
      "done",
    ]);
    // … and the inner code body IS mounted (expanded).
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("clicking the run header toggles inner rows (expanded by default, click collapses)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    // Default expanded: inner code row is mounted.
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
    fireEvent.click(runToggle());
    // After click, collapsed: inner code row unmounts.
    expect(screen.queryByTestId("python-code")).toBeNull();
    // Click again -> re-expanded.
    fireEvent.click(runToggle());
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("a lone secondary item is wrapped in a work block (always collapsible)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "0.0", kind: "agent_chat", payload: "a" }),
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "agent:7", payload: "ping" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "b" }),
        ]}
      />,
    );
    // The lone inter-agent message is wrapped in a work block.
    const runHeader = runToggle();
    expect(runHeader.textContent).toContain("1 agent message");
    // The inner card still renders its header ("Agent 7").
    expect(screen.getByText("Agent 7")).toBeTruthy();
  });

  it("streaming items join the same work block — first chunk lands in a wrapper immediately", () => {
    // When the first item is streaming, it should land directly inside a work
    // block (no bare-then-wrap layout shift). Both items are in the same turn.
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1-done" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1-live" }),
        ]}
      />,
    );
    // Both items are in the same work block. The turn is auto-expanded
    // (turnActive) so both inner rows are mounted.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("t1-done")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("c1-live");
  });

  it("streaming items are always groupable — no peeling, all in one work block", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1" }),
          makeItem({ item_id: "1.2", kind: "agent_reasoning", payload: "t2-live" }),
        ]}
      />,
    );
    // All three items fold into a single work block. The turn is auto-expanded
    // (turnActive) so all inner rows are mounted.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("t1")).toBeTruthy();
    expect(screen.getByText("t2-live")).toBeTruthy();
  });

  it("detailsMode='none' → runs start collapsed (toggle dim, inner rows not mounted)", () => {
    setToggleState({ detailsMode: 'none' });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    // Turn block exists (always grouped) but is collapsed.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    // Inner rows are not mounted.
    expect(screen.queryByTestId("python-code")).toBeNull();
  });

  it("interrupted items stay in the work block — not peeled out", () => {
    // A mid-stream disconnect clears turnActive; the half-finished bubble
    // stays inside the same work block and stays visible because the block
    // remains expanded (the turn was last-active when it was interrupted).
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1" }),
          makeItem({
            item_id: "1.2",
            kind: "agent_reasoning",
            payload: "t2-cut",
            partial: true,
            interrupted: true,
          }),
        ]}
      />,
    );
    // All three items fold into one work block. The auto-expand only applies
    // when turnActive is true, so with turnActive=false and showSteps defaulting
    // to true, the block is expanded via the default.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/t2-cut/)).toBeTruthy();
    expect(screen.getByText(/reconnects resume receiving/)).toBeTruthy();
  });

  it("a turn block carries a stable data-item-id (its first member id)", () => {
    // The load-older scroll anchor needs a stable node even when the topmost
    // content is a run whose inner rows are unmounted.
    const { container } = render(
      <TimelineView
        items={[
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    const wrapper = container.querySelector('[data-item-id="3.0"]');
    expect(wrapper).toBeTruthy();
    // The node is the work-block wrapper (holds the aggregate toggle).
    expect(wrapper?.textContent).toContain("thinking");
    // Default expanded (detailsMode='all'). Inner code row is mounted.
    expect(wrapper?.querySelector('[data-expanded="true"]')).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("a turn block also stamps data-turn-member-ids for EVERY member, surviving a run-extending load-older prepend", () => {
    // "3.0" starts as the run's own first member — found via data-item-id,
    // same as the test above. A load-older fetch then prepends an older
    // secondary item ("2.0") that extends the SAME run's front: "3.0" is
    // still there, just no longer first. The load-older scroll-anchor lookup
    // in TimelineView falls back to data-turn-member-ids for this case.
    const { container, rerender } = render(
      <TimelineView
        items={[
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    expect(container.querySelector('[data-item-id="3.0"]')).toBeTruthy();

    rerender(
      <TimelineView
        items={[
          makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "older" }),
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    // The run's first member moved to the newly-prepended older item.
    // When expanded, individual items inside the run still carry their
    // data-item-id, so "3.0" is found as an inner card.
    expect(container.querySelector('[data-item-id="2.0"]')).toBeTruthy();
    // The run wrapper carries data-turn-member-ids for EVERY member.
    const run = container.querySelector('[data-turn-member-ids~="3.0"]');
    expect(run).toBeTruthy();
    expect(run?.getAttribute("data-turn-member-ids")).toBe("2.0 3.0 3.1");
  });

  it('detailsMode="last" — clicking the last turn header toggles expansion', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    // In Last mode with turnActive, the last turn is expanded by default.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");

    // Click to collapse the last turn.
    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();

    // Click again to re-expand the last turn.
    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  // Bug regression (#510): the last turn auto-expands while streaming and
  // auto-collapses once the agent goes idle. Clicking its header AFTER
  // streaming completes must expand it again. The old override convention
  // ("membership = opposite of the default") broke exactly here: the last
  // turn's default itself flips when turnActive ends, so the formula
  // hardcoded the override to collapsed and every click did nothing.
  it('detailsMode="last" — clicking the last turn header after streaming completes expands it', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    // No turnActive → the last turn auto-collapsed.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();

    // Click to expand the completed turn — the reported bug: this did nothing.
    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");

    // Click again to collapse it.
    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
  });

  // Bug regression (#510): a user who collapses the streaming last turn
  // mid-flight pins that choice — the turn stays collapsed when streaming
  // ends, and a later click expands it (previously the collapse click stored
  // an override that permanently glued the turn shut).
  it('detailsMode="last" — collapse pinned mid-stream survives the turn going idle, then a click expands', () => {
    setToggleState({ detailsMode: "last" });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
    ];
    const { rerender } = render(<TimelineView turnActive items={items} />);
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");

    // Collapse the streaming turn.
    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");

    // Streaming stops — the pin keeps it collapsed (no surprise re-expand).
    rerender(<TimelineView items={items} />);
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");

    // Now a click expands it.
    fireEvent.click(runToggle());
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
  });

  it('detailsMode="last" — within expanded turn, all sub-items are visible (not just last)', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think-first" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "code-mid" }),
          makeItem({ item_id: "1.2", kind: "agent_reasoning", payload: "think-last" }),
        ]}
      />,
    );
    // In Last mode with turnActive, the last turn is expanded. ALL items within it should
    // be visible, not just the last one.
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    // All three sub-items should be visible.
    expect(screen.getByText("think-first")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("code-mid");
    expect(screen.getByText("think-last")).toBeTruthy();
  });

  // Bug regression (#659): a detail block expands ALL its inner content in one
  // click — "no per-category expansion". ff15af78 split the streaming case
  // from the click case so a user-clicked middle turn revealed only its child
  // summaries (each inner block stayed collapsed for a second click) — the
  // "detail block only expands specific categories" complaint. Re-unified:
  // any expanded turn — streaming auto-expand or a user click — opens its
  // inner blocks too; per-card collapse remains available via an inner
  // header click.
  it('detailsMode="last" — clicking a middle turn opens it with ALL inner blocks expanded', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "older-think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "older-code" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "chat" }),
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "newer-think" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "newer-code" }),
        ]}
      />,
    );
    // Two turns: [older-think, older-code] · <chat> · [newer-think, newer-code].
    // With turnActive, only the LAST turn streaming-auto-expands — and it is the
    // only turn whose inner blocks open too.
    const runHeaders = screen.getAllByTestId("turn-toggle");
    expect(runHeaders).toHaveLength(2);

    // Older (middle) turn collapsed; newer (last) turn expanded with its inner
    // blocks fully open.
    expect(runHeaders[0].getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("older-think")).toBeNull();
    expect(runHeaders[1].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("newer-think")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("newer-code");

    // Click the older (middle) turn open: ALL its inner blocks expand in the
    // same click — both bodies mount, not just the card headers.
    fireEvent.click(runHeaders[0]);
    expect(runHeaders[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("older-think")).toBeTruthy();
    expect(screen.getAllByTestId("python-code")).toHaveLength(2);
    // The streaming last turn is untouched by the middle-turn click.
    expect(runHeaders[1].getAttribute("aria-expanded")).toBe("true");

    // Per-card collapse still works: clicking one inner header closes just
    // that card.
    fireEvent.click(screen.getAllByTestId("card-toggle")[0]); // older-think
    expect(screen.queryByText("older-think")).toBeNull();
    expect(screen.getAllByTestId("python-code")).toHaveLength(2); // both code cards
  });
});

// ---------------------------------------------------------------------------
// Detail-block duration display — regression tests for the four Last-mode
// symptoms (2026-07: live code clock dead, streaming jitter, first-block
// turns showing no timer, system-note-only turns rendering blank).
// ---------------------------------------------------------------------------
describe("detail-block duration display (Last mode)", () => {
  const runToggle = () => screen.getByTestId("turn-toggle");

  // Symptom 1: during streaming the code card must tick a live
  // "Writing code for Xs" inside the expanded last turn.
  it("streaming code block inside the active last turn shows a live 'Writing code for'", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        streamingCode
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "on it" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_code",
            payload: "x = 1",
            codeStartedAt: Date.now() - 3_000,
          }),
        ]}
      />,
    );
    // Last turn auto-expanded while active; the code card header ticks live.
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/Writing code for/)).toBeTruthy();
  });

  // Symptom 1 (turn header): the work block itself ticks "Working for Xs"
  // while the turn is active.
  it("active last turn header shows a live 'Working for'", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hmm" }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for");
  });

  // Symptom 3 (live half): the live clock must run from the turn's FIRST
  // block — even before any thinking/code/output item lands (e.g. a system
  // wake-up waiting on the LLM's first token).
  it("live 'Working for' does not wait for a work item — a system-only active turn still ticks", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "schedule:1", payload: "wake" }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for");
  });

  // Symptom 3 (completed half): a turn that was one continuous code block
  // shows both "Worked for" (from the block's own committed duration) and
  // "Wrote code for" (from the backend code_elapsed_ms).
  it("completed single-code-block turn shows 'Worked for' and 'Wrote code for'", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_code",
            payload: "x = 1",
            code_elapsed_ms: 5_000,
          }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const header = runToggle();
    expect(header.textContent).toContain("Worked for 5s");
    expect(header.textContent).toContain("Wrote code for 5s");
  });

  // Symptom 4: a turn holding only a system note must never render a blank
  // header — it summarizes as "1 system note" (and shows no work timer,
  // preserving #764's intent).
  it("system-note-only turn shows '1 system note', not a blank header and no 'Worked for'", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({ item_id: "2.0", kind: "system_marker", source: "sdk_hint", payload: "note text" }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const header = runToggle();
    expect(header.textContent).toContain("1 system note");
    expect(header.textContent).not.toContain("Worked for");
  });

  // #1052: the live and completed halves of the timer measured different
  // things, so the header visibly DROPPED at turn end — "Working for 6m"
  // settling to "Worked for 17s". Both halves now read the same basis (the
  // sum of the turn's block durations), and the live half adds only the
  // in-flight block's elapsed on top.
  it("live timer excludes the idle gap the turn spans, so it does not drop at turn end", () => {
    setToggleState({ detailsMode: "last" });
    const now = Date.now();
    // The turn opens on a scheduled wake-up six minutes ago and holds one
    // 17s code block: 6m of wall-clock over 17s of work.
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
      makeItem({
        item_id: "2.0",
        kind: "inbound_chat",
        source: "schedule:1",
        payload: "wake",
        created_at: new Date(now - 6 * 60_000).toISOString(),
      }),
      makeItem({
        item_id: "3.0",
        kind: "agent_code",
        payload: "x = 1",
        code_elapsed_ms: 17_000,
        created_at: new Date(now - 1_000).toISOString(),
      }),
    ];
    const { rerender } = render(<TimelineView turnActive items={items} />);
    expect(runToggle().textContent).toContain("Working for 17s");
    expect(runToggle().textContent).not.toContain("6m");

    // Turn ends: same number, different verb — no drop.
    rerender(<TimelineView items={items} />);
    expect(runToggle().textContent).toContain("Worked for 17s");
  });

  // The live half is committed work PLUS the streaming block's elapsed, and
  // that elapsed is the very value the sub-block line already shows — so the
  // header is always the sum of the line beneath it.
  it("live timer adds the streaming block's elapsed to the committed total", () => {
    setToggleState({ detailsMode: "last" });
    const now = Date.now();
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hmm",
            reasoning_ms: 10_000,
            created_at: new Date(now - 3_500).toISOString(),
          }),
          makeItem({
            item_id: "3.0",
            kind: "agent_code",
            payload: "x = 1",
            codeStartedAt: now - 3_000,
            created_at: new Date(now - 3_000).toISOString(),
          }),
        ]}
      />,
    );
    // 10s committed thinking + 3s of code still being written.
    const header = runToggle().textContent;
    expect(header).toContain("Working for 13s");
    expect(header).toContain("Thought for 10s");
    expect(header).toContain("Wrote code for 3s");
  });

  // The live gate no longer keys on the turn's first stamped created_at: the
  // displayed value stopped deriving from that anchor, so a turn whose members
  // are all unstamped (an agent's opening turn leads with the system prompt,
  // which the backend emits with created_at null) still shows its timer.
  it("live timer shows on a turn with no stamped member at all", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hmm",
            reasoning_ms: 4_000,
            created_at: null,
          }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for 4s");
  });

  // Symptom 2 (jitter): with a steady mid-turn signal the expanded last turn
  // must not collapse when only its content grows across commits; it
  // collapses once a primary reply lands below it (turn over in the data).
  it("active last turn stays expanded across streaming commits; collapses when a reply lands below", () => {
    setToggleState({ detailsMode: "last" });
    const base = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
      makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "hmm" }),
    ];
    const { rerender } = render(<TimelineView turnActive items={base} />);
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    // A new secondary item lands (code follows thinking) — still expanded.
    rerender(
      <TimelineView
        turnActive
        items={[...base, makeItem({ item_id: "2.1", kind: "agent_code", payload: "x=1" })]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    // The final reply lands below the turn → the turn is no longer the last
    // group and auto-collapses even while turnActive is still winding down.
    rerender(
      <TimelineView
        turnActive
        items={[
          ...base,
          makeItem({ item_id: "2.1", kind: "agent_code", payload: "x=1" }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
  });
});

describe("Marker contract: every dispatch-set source renders without the red alarm", () => {
  // The backend NoteTag enum (shared/message_kwargs.py) is asserted to be a
  // subset of these dispatch sets by tests/test_lint_marker_contract.py (CI
  // backend job). This test closes the loop on the frontend side: every
  // member of each exported set actually renders as its intended chip and
  // NEVER falls to the UnknownMarkerChip red alarm — the fail-loud path the
  // user hit as "UNRECOGNIZED SYSTEM_MARKER (FRONTEND NOT ADAPTED)" (#1017).
  // Iterating the exported sets (not a hand-written list) keeps the test in
  // lockstep with the renderer's own dispatch.
  const knownSources = [
    ...LIFECYCLE_TAGS,
    ...Array.from(MEMORY_SOURCES),
    ...Array.from(NOTE_SOURCES),
  ];

  it.each(knownSources)("source=%s renders its chip, never the alarm", (source) => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source,
            payload: "[system] contract-test payload",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });

  it("positive control: an unknown source still renders the red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_future_tag",
            payload: "x",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

// #1016 — a touch drag must not be fought by the streaming pin. On touch
// devices a slow scroll-up during streaming was canceled every frame by the
// ResizeObserver pin (the position rule measured against the just-pinned
// bottom, so the drag could never accumulate past the touch bottom zone):
// the timeline stayed glued to the bottom and the scroll-to-bottom button
// never appeared. The controller-level rules live in sticky.test.ts; this
// test pins the component wiring — touchstart/touchend on the viewport feed
// the controller, and the RO pin is suppressed while a drag is active.
describe("touch drag vs streaming pin (#1016)", () => {
  // happy-dom's ResizeObserver never fires; drive the component's RO
  // manually so the pin path is exercised deterministically.
  let roCallback: ResizeObserverCallback | null = null;
  let roObserved: Element[] = [];

  beforeEach(() => {
    roCallback = null;
    roObserved = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: ResizeObserverCallback) {
          roCallback = cb;
        }
        observe(el: Element) {
          roObserved.push(el);
        }
        /* eslint-disable @typescript-eslint/no-empty-function -- observer teardown is a no-op in the manual stub */
        unobserve() {}
        disconnect() {}
        /* eslint-enable @typescript-eslint/no-empty-function */
        takeRecords() {
          return [];
        }
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // Drive one RO callback with the given geometry (the component's callback
  // reads the live viewport, so defineProperty the numbers first).
  const fireRO = () => {
    expect(roCallback).not.toBeNull();
    roCallback!([], {} as ResizeObserver);
  };

  const viewportGeom = (viewport: HTMLElement, scrollHeight: number, clientHeight: number) => {
    Object.defineProperty(viewport, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: clientHeight, configurable: true });
  };

  it("a layout change during an active touch drag does not pin; release beyond the zone stops following", () => {
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    // Viewport 600 tall, content 1000, user pinned at the bottom (400).
    viewportGeom(viewport, 1000, 600);
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll")); // position rule → sticky
    fireRO(); // initial RO → pin (already at bottom, no-op)

    // The user starts dragging up.
    viewport.dispatchEvent(new Event("touchstart"));
    viewport.scrollTop = 370;
    viewport.dispatchEvent(new Event("scroll")); // 30px up — inside the zone
    expect(viewport.scrollTop).toBe(370); // drag is honored

    // A chunk lands mid-drag (content grows to 1150): with the fix the RO
    // must NOT pin the viewport back to the bottom under the user's finger.
    viewportGeom(viewport, 1150, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);

    // Another chunk while still dragging — still no pin.
    viewportGeom(viewport, 1300, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);

    // The finger lifts beyond the zone (dist = 1300 - 370 - 600 = 330) —
    // following stops; the next chunk must not yank the reader back.
    viewport.dispatchEvent(new Event("touchend"));
    viewportGeom(viewport, 1450, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);
  });

  it("the pin returns once the user drags back to the bottom and releases there", () => {
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    viewportGeom(viewport, 1000, 600);
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll"));

    viewport.dispatchEvent(new Event("touchstart"));
    viewport.scrollTop = 200; // dragged far up
    viewport.dispatchEvent(new Event("scroll")); // position rule unsticks
    viewport.dispatchEvent(new Event("touchend"));

    // Back to the bottom (re-stick via the position rule).
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll"));
    // Growth now pins again. (happy-dom does not clamp scrollTop on
    // assignment; a real browser clamps the pin's scrollTop =
    // scrollHeight write to scrollHeight - clientHeight = 600. The
    // assertion is on the pin HAVING happened — scrollTop moved from
    // 400 to the written scrollHeight.)
    viewportGeom(viewport, 1200, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(1200); // pinned (real browser: 600)
  });
});

describe("streamingParseIntervalMs (adaptive stream-parse window, user report 2026-09-06)", () => {
  it("keeps the base 40ms window for short payloads", () => {
    expect(streamingParseIntervalMs(0)).toBe(40);
    expect(streamingParseIntervalMs(100)).toBe(40);
    expect(streamingParseIntervalMs(2400)).toBe(40);
  });
  it("widens one SSE window per 2400 bytes", () => {
    expect(streamingParseIntervalMs(2401)).toBe(80);
    expect(streamingParseIntervalMs(4800)).toBe(80);
    expect(streamingParseIntervalMs(4801)).toBe(120);
    expect(streamingParseIntervalMs(20_000)).toBe(360);
  });
  it("caps at 1000ms — a 300-line block re-parses ~1x/s", () => {
    expect(streamingParseIntervalMs(57_600)).toBe(960); // 24 steps × 40ms ≈ 1Hz
    expect(streamingParseIntervalMs(60_000)).toBe(1000);
    expect(streamingParseIntervalMs(200_000)).toBe(1000);
  });
});
