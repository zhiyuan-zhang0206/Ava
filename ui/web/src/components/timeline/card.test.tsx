/**
 * card.tsx — unit tests for MessageCard, CardHeader, messageCardConfig,
 * and the rich summary components.
 *
 * Historical quarantine root cause: live-clock renders here (useNow's 100ms
 * setInterval) leaked past the file's end because `globals: false` prevented
 * automatic cleanup. A tick after happy-dom teardown surfaced as an unhandled
 * "ReferenceError: window is not defined" from react-dom's scheduler under
 * CI's parallel forks. The fix landed in vitest.setup.ts as
 * afterEach(cleanup), which unmounts and clears those intervals per test.
 * De-quarantined 2026-08-24.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CardHeader, MessageCard, messageCardConfig, type CardConfig } from "./card";
import type { BackendTimelineItem } from "@/lib/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function item(
  kind: BackendTimelineItem["kind"],
  overrides: Partial<BackendTimelineItem> = {},
): BackendTimelineItem {
  return {
    item_id: "0.0",
    kind,
    source: null,
    payload: "",
    created_at: new Date().toISOString(),
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

// eslint-disable-next-line @typescript-eslint/no-empty-function
const noop = () => {};

// CardHeader now reads user settings via useUserSettings, which requires a
// React Query context. Seed with default settings including the new
// display.show_timestamp_weekday flag (default true).
function renderWithQuery(ui: React.ReactElement, overrides: Record<string, unknown> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(["user-settings"], {
    "display.show_timestamp_weekday": true,
    ...overrides,
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// ---------------------------------------------------------------------------
// messageCardConfig
// ---------------------------------------------------------------------------

describe("messageCardConfig", () => {
  it("returns null for ephemeral system_marker (no card)", () => {
    const cfg = messageCardConfig(
      item("system_marker", { source: null, payload: "error:boom" }),
    );
    expect(cfg).toBeNull();
  });

  it("system_prompt → rich=system_prompt, fixed default collapsed", () => {
    const cfg = messageCardConfig(item("system_prompt", { payload: "hello" }));
    expect(cfg).not.toBeNull();
    expect(cfg!.rich).toBe("system_prompt");
    expect(cfg!.fixedDefault).toBe(false);
  });

  it("agent_reasoning → rich=reasoning", () => {
    const cfg = messageCardConfig(item("agent_reasoning"));
    expect(cfg!.rich).toBe("reasoning");
    expect(cfg!.fixedDefault).toBe(true);
  });

  it("agent_code → rich=code", () => {
    const cfg = messageCardConfig(item("agent_code"));
    expect(cfg!.rich).toBe("code");
  });

  it("attach → paperclip card, open by default", () => {
    const cfg = messageCardConfig(item("attach"));
    expect(cfg).not.toBeNull();
    expect(cfg!.icon?.displayName).toBe("Paperclip");
    expect(cfg!.titleKey).toBe("timeline.attachedFiles");
    expect(cfg!.fixedDefault).toBe(true);
    expect(cfg!.headerTs).toBe(true);
  });

  it("code_output → rich=output", () => {
    const cfg = messageCardConfig(item("code_output"));
    expect(cfg!.rich).toBe("output");
  });

  it("agent_chat → no rich, title='Ava ▸'", () => {
    const cfg = messageCardConfig(item("agent_chat"));
    expect(cfg!.rich).toBeNull();
    expect(cfg!.title).toBe("Ava ▸");
  });

  it("agent_chat partial → title includes '(streaming…)'", () => {
    const cfg = messageCardConfig(item("agent_chat", { partial: true }));
    expect(cfg!.title).toContain("(streaming…)");
  });

  it("agent_chat interrupted → title includes '(streaming interrupted)'", () => {
    const cfg = messageCardConfig(item("agent_chat", { interrupted: true }));
    expect(cfg!.title).toContain("(streaming interrupted)");
  });

  it("inbound_chat source=user → Human, gray border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "user" }));
    expect(cfg!.title).toBe("Human");
    expect(cfg!.border).toContain("gray");
  });

  it("inbound_chat source=agent:42 → Agent 42, violet border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "agent:42" }));
    expect(cfg!.title).toBe("Agent 42");
    expect(cfg!.border).toContain("violet");
  });

  it("inbound_chat ui:page → Human", () => {
    const cfg = messageCardConfig(
      item("inbound_chat", { source: "ui:page:foo" }),
    );
    expect(cfg!.title).toBe("Human");
  });

  it("inbound_chat unknown source → System, sky border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "kernel" }));
    expect(cfg!.title).toBe("System");
    expect(cfg!.border).toContain("sky");
  });

  it("inbound_compact → differentiated labels, sky border", () => {
    const cfgSummary = messageCardConfig(item("inbound_compact_summary"));
    expect(cfgSummary!.title).toBe("Compact summary");
    expect(cfgSummary!.border).toContain("sky");

    const cfgRequest = messageCardConfig(item("inbound_compact_request"));
    expect(cfgRequest!.title).toBe("Compact request");
    expect(cfgRequest!.border).toContain("sky");
  });

  it("system_marker lifecycle_terminate → Terminated, rose border", () => {
    const cfg = messageCardConfig(
      item("system_marker", { source: "lifecycle_terminate" }),
    );
    expect(cfg!.title).toBe("Terminated");
    expect(cfg!.border).toContain("rose");
  });

  it("system_marker memory → Memory, violet border", () => {
    for (const src of ["memory", "agent_memory"]) {
      const cfg = messageCardConfig(item("system_marker", { source: src }));
      expect(cfg!.title).toBe("Memory");
      expect(cfg!.border).toContain("violet");
    }
  });

  it("system_marker note → Note", () => {
    for (const src of ["sdk_hint", "agent_id", "context", "heartbeat", "security"]) {
      const cfg = messageCardConfig(item("system_marker", { source: src }));
      expect(cfg!.title).toBe("Note");
    }
  });
});

// ---------------------------------------------------------------------------
// MessageCard
// ---------------------------------------------------------------------------

describe("MessageCard", () => {
  const cfg: CardConfig = {
    rich: null,
    icon: null,
    title: "Test",
    border: "border-red-500",
    bg: "bg-red-50",
    fixedDefault: true,
    headerTs: false,
  };

  it("renders children inside colored frame", () => {
    const { container } = render(
      <MessageCard config={cfg}>
        <div data-testid="child">hello</div>
      </MessageCard>,
    );
    expect(container.firstElementChild?.className).toContain("border-l-2");
    expect(container.firstElementChild?.className).toContain("border-red-500");
  });

  it("no actions prop → mounts no overlay DOM at all (not just hidden)", () => {
    const { container, queryByText } = render(
      <MessageCard config={cfg}>
        <div>hello</div>
      </MessageCard>,
    );
    expect(queryByText("act")).toBeNull();
    // The frame itself must not opt into the named hover group when there's
    // nothing to reveal.
    expect(container.firstElementChild?.className).not.toContain("group/msgcard");
  });

  it("actions prop → overlay renders, pinned inside the frame (relative/absolute), and opts the frame into group/msgcard", () => {
    const { container, getByText } = render(
      <MessageCard config={cfg} actions={<span>act</span>}>
        <div>hello</div>
      </MessageCard>,
    );
    expect(getByText("act")).toBeTruthy();
    const frame = container.firstElementChild;
    expect(frame?.className).toContain("relative");
    expect(frame?.className).toContain("group/msgcard");
    // The overlay is absolutely positioned inside the frame, not a sibling
    // that pushes layout — it must be a descendant of the frame div, and the
    // strip itself must not capture pointer events (only the pill's hover/
    // focus-visible state does).
    const strip = frame?.querySelector(".absolute.inset-x-0.bottom-0");
    expect(strip).toBeTruthy();
    expect(strip?.className).toContain("pointer-events-none");
    // The pill itself must ALSO default to pointer-events-none while
    // invisible (opacity-0 alone does not disable hit-testing) — otherwise a
    // tap in this corner on a touch device (no real :hover) would silently
    // hit an unseen button. pointer-events-auto only switches on together
    // with the same hover/focus-within variant that raises opacity.
    const pill = strip?.firstElementChild;
    expect(pill?.className).toContain("pointer-events-none");
    expect(pill?.className).toContain("group-hover/msgcard:pointer-events-auto");
    expect(pill?.className).toContain("focus-within:pointer-events-auto");
    // Touch/coarse-pointer devices have no real :hover to gate on — they opt
    // out of the reveal gate entirely (always visible + tappable there).
    expect(pill?.className).toContain("pointer-coarse:opacity-100");
    expect(pill?.className).toContain("pointer-coarse:pointer-events-auto");
  });
});

// ---------------------------------------------------------------------------
// CardHeader
// ---------------------------------------------------------------------------

describe("CardHeader", () => {
  const baseItem = item("agent_chat", { payload: "test" });

  it("renders title and chevron for a non-rich config", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Ava ▸");
  });

  it("uses sans typography and the named small-size token for its UI header", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    const button = container.querySelector("button")!;
    expect(button.classList).toContain("font-sans");
    expect(button.classList).toContain("text-xs");
    expect(button.classList).not.toContain("font-mono");
  });

  it("collapsed state → aria-expanded=false, data-expanded=false, no title (no hover tooltip)", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("data-expanded")).toBe("false");
    expect(btn.getAttribute("title")).toBeNull();
  });

  it("expanded state → aria-expanded=true, data-expanded=true, no title (no hover tooltip)", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(btn.getAttribute("data-expanded")).toBe("true");
    expect(btn.getAttribute("title")).toBeNull();
  });

  it("renders rich summary for system_prompt kind", () => {
    const sysItem = item("system_prompt", {
      payload: "line1\nline2\nline3",
    });
    const cfg = messageCardConfig(sysItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={sysItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("System prompt");
    expect(container.textContent).toContain("3 lines");
  });

  it("renders rich summary for agent_reasoning with committed duration", () => {
    const rItem = item("agent_reasoning", {
      payload: "thinking...",
      reasoning_ms: 8000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thought for 8s");
  });

  it("reasoning with reasoning_tokens shows token count", () => {
    const rItem = item("agent_reasoning", {
      payload: "x",
      reasoning_ms: 5000,
      reasoning_tokens: 1500,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("1.5k tokens");
  });

  it("reasoning with live clock (reasoningStartedAt)", () => {
    const rItem = item("agent_reasoning", {
      payload: "t",
      reasoningStartedAt: Date.now() - 3000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thinking for");
  });

  it("reasoning with reasoningElapsedMs (frozen clock)", () => {
    const rItem = item("agent_reasoning", {
      payload: "t",
      reasoningElapsedMs: 12000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thought for 12s");
  });

  it("reasoning without timing → bare 'Thinking'", () => {
    const rItem = item("agent_reasoning", { payload: "old" });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thinking");
  });

  it("agent_code with SDK calls shows method names", () => {
    const cItem = item("agent_code", {
      payload: "ava.files.read('x')",
      sdk_calls: [
        { method: "files.read", count: 2 },
        { method: "shell.run", count: 1 },
      ],
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("files.read");
    expect(container.textContent).toContain("× 2");
    expect(container.textContent).toContain("shell.run");
  });

  // Regression (symptom 1): the live "Writing code for Xs" clock. The reducer
  // keeps codeStartedAt on the streaming agent_code item (timeline.ts
  // code_start); the header must tick a live label off it.
  it("agent_code streaming (codeStartedAt, no committed duration) → live 'Writing code for'", () => {
    const cItem = item("agent_code", {
      payload: "x = 1",
      codeStartedAt: Date.now() - 2000,
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Writing code for");
  });

  it("agent_code with committed code_elapsed_ms → past-tense 'Wrote code for'", () => {
    const cItem = item("agent_code", {
      payload: "x = 1",
      code_elapsed_ms: 5000,
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 5s");
  });

  // Regression (Bug 2): a committed code_elapsed_ms of 0 — the backend value when
  // a provider delivers the whole tool-call args in a single delta (first==last
  // code-fragment timestamp) — must still show a duration, floored at 0.1s, NOT
  // be dropped. The old `> 0` guard hid the whole label for these blocks while
  // thinking/output showed theirs.
  it("agent_code with committed code_elapsed_ms === 0 (single-chunk args) → floored 'Wrote code for 0.1s'", () => {
    const cItem = item("agent_code", { payload: "x = 1", code_elapsed_ms: 0 });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 0.1s");
  });

  it("agent_code falls back to frozen codeElapsedMs when code_elapsed_ms absent", () => {
    const cItem = item("agent_code", { payload: "x = 1", codeElapsedMs: 3000 });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 3s");
  });

  // Bug 2, all three inner block kinds: a completed block with a 0ms duration
  // shows the 0.1s floor rather than dropping the timer — thinking / code /
  // output stay consistent (formatDuration floors every value at 0.1s).
  it("committed 0ms duration → all three inner blocks show a floored timer", () => {
    const cases: [BackendTimelineItem["kind"], Partial<BackendTimelineItem>, string][] = [
      ["agent_reasoning", { reasoning_ms: 0 }, "Thought for 0.1s"],
      ["agent_code", { code_elapsed_ms: 0 }, "Wrote code for 0.1s"],
      ["code_output", { exec_ms: 0 }, "Ran in 0.1s"],
    ];
    for (const [kind, overrides, expected] of cases) {
      const it = item(kind, { payload: "x", ...overrides });
      const cfg = messageCardConfig(it)!;
      const { container } = renderWithQuery(
        <CardHeader item={it} config={cfg} expanded={false} onToggle={noop} />,
      );
      expect(container.textContent).toContain(expected);
    }
  });

  it("agent_code without SDK calls → line count", () => {
    const cItem = item("agent_code", {
      payload: "x = 1\ny = 2\nz = 3",
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("3 lines of code");
  });

  it("code_output with exec_ms shows duration", () => {
    const oItem = item("code_output", {
      payload: "line1\nline2",
      exec_ms: 8000,
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Ran in 8s");
    expect(container.textContent).toContain("2 lines");
  });

  it("code_output with error → shows error prefix", () => {
    const oItem = item("code_output", {
      payload: "ValueError: something went wrong",
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Error");
  });

  it("code_output live execution (execStartedAt, no exec_ms)", () => {
    const oItem = item("code_output", {
      payload: "running...",
      execStartedAt: Date.now() - 2000,
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Running for");
  });
});
