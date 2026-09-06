// HomePage tests — mock all use-* hooks and child components as stubs to
// test top-level wiring and state flow (send calls the store's
// requestScrollToBottom, handleSend errors bubble to toast, composerMode
// tri-state).
//
// Hooks have too many heavy dependencies (useAgents+useTimeline+useTokenUsage+
// useEventStream+useStore); running them for real triggers SSE / fetch /
// Zustand subscribe side effects. Stubs keep HomePage's own
// useState/useEffect/useCallback transformation logic testable.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { LAYOUT_INVARIANTS, LAYOUT_VIEWPORT_TIERS } from "@/lib/layout";

import type { AgentRow } from "@/lib/types";
import type { ConnectionState } from "@/lib/use-timeline";

// -- Hook + child-component mocks --
// hoisted state lets individual tests change hook return values
// (vi.mock factory runs only once)

const hooksState = {
  isLarge: true,
  // Desktop viewport by default (mirrors useMediaQuery's post-mount state);
  // false = narrow/mobile (<768px, task #805).
  isDesktop: true,
  agents: [] as AgentRow[],
  activeId: null as number | null,
  forkPending: false,
  toast: null as string | null,
  timelineItems: [] as unknown[],
  connectionState: "open" as ConnectionState,
  turnActive: false,
  spawn: vi.fn().mockResolvedValue(7),
  fork: vi.fn().mockResolvedValue(8),
  terminate: vi.fn().mockResolvedValue(undefined),
  restart: vi.fn().mockResolvedValue(undefined),
  resurrect: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
  focusComposer: vi.fn(),
  setMobileSidebarOpen: vi.fn(),
  setMobileInspectorOpen: vi.fn(),
  setInspectorHours: vi.fn(),
  requestScrollToBottom: vi.fn(),
  setSetting: vi.fn(),
  // DB-backed user settings — timeline-width ratio is the one HomeContent reads.
  settings: { "display.timeline_width_ratio": 0.4 } as Record<string, unknown>,
};

vi.mock("@/lib/use-agents", () => ({
  useAgents: () => ({
    agents: hooksState.agents,
    activeId: hooksState.activeId,
    setActiveId: vi.fn(),
    pendingActions: {},
    pendingSpawnCount: 0,
    forkPending: hooksState.forkPending,
    spawn: hooksState.spawn,
    fork: hooksState.fork,
    terminate: hooksState.terminate,
    restart: hooksState.restart,
    resurrect: hooksState.resurrect,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/lib/use-timeline", () => ({
  useTimeline: () => ({
    items: hooksState.timelineItems,
    streamingCode: false,
    connectionState: hooksState.connectionState,
    turnActive: hooksState.turnActive,
    isLoading: false,
    isRefetching: false,
  }),
}));

vi.mock("@/lib/use-token-usage", () => ({
  useTokenUsage: () => ({
    contextTokens: 0,
    maxContextTokens: 0,
    softCompactTokens: 0,
    hardCompactTokens: 0,
  }),
}));

vi.mock("@/lib/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: hooksState.settings,
    setSetting: hooksState.setSetting,
    isLoading: false,
  }),
}));

// The cluster-health poller + the SSE-disconnect banner moved out of the home
// page to the app root (components/app-connection-banner.tsx) / timeline
// (components/connection-notice.tsx), so HomePage no longer mounts either —
// their behavior is covered by use-cluster-health.test.tsx +
// connection-notice.test.tsx + useEventStream.test.ts.

vi.mock("@/lib/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  AgentEventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: vi.fn(),
  useAgentEventStream: vi.fn(),
}));

vi.mock("@/lib/store", () => ({
  useStore: <T,>(selector: (s: unknown) => T): T => {
    const fakeState = {
      toast: hooksState.toast,
      showToast: hooksState.showToast,
      focusComposer: hooksState.focusComposer,
      forkPending: hooksState.forkPending,
      composerFocusToken: 0,
      setMobileSidebarOpen: hooksState.setMobileSidebarOpen,
      mobileInspectorOpen: false,
      setMobileInspectorOpen: hooksState.setMobileInspectorOpen,
      inspectorHours: 24,
      setInspectorHours: hooksState.setInspectorHours,
      agents: hooksState.agents,
    };
    return selector(fakeState);
  },
}));

// Breakpoint: tests default to desktop (isLarge = true). The inspector
// mount-reset is desktop-only (task #793), so the mobile tests flip this.
// R4 layer 4: the page consumes useBreakpoint — the single breakpoint source.
vi.mock("@/lib/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: hooksState.isDesktop ? "xl" : "xs",
    isNarrow: !hooksState.isDesktop,
    isLarge: hooksState.isLarge,
  }),
}));

// requestScrollToBottom moved to the timeline store (useTimelineStore) when the
// timeline split out of the app store; page.tsx reads it from there on send.
vi.mock("@/lib/timeline-store", () => ({
  useTimelineStore: <T,>(selector: (s: unknown) => T): T => {
    const fakeState = {
      requestScrollToBottom: hooksState.requestScrollToBottom,
    };
    return selector(fakeState);
  },
}));

vi.mock("@/lib/api", () => ({
  API_BASE: "",
  MessageDeliveryUnknownError: class MessageDeliveryUnknownError extends Error {
    constructor(readonly clientMessageId: string) {
      super("delivery unconfirmed");
    }
  },
  api: {
    sendMessage: vi.fn().mockResolvedValue(undefined),
    cancel: vi.fn().mockResolvedValue(undefined),
    uploadFiles: vi.fn().mockResolvedValue(undefined),
    listPages: vi.fn().mockResolvedValue([]),
  },
}));



// Child-component stubs — render a recognizable testid + expose key props for assertions
vi.mock("@/components/agent-sidebar", () => ({
  AgentSidebar: ({ onSpawn }: { onSpawn: (opts: { machine?: string; model?: string }) => void }) => (
    <button data-testid="sidebar-spawn" onClick={() => { onSpawn({}); }}>spawn</button>
  ),
}));

vi.mock("@/components/header-bar", () => ({
  HeaderBar: ({
    label,
    children,
    maxWidthCss,
  }: {
    label: string;
    children?: React.ReactNode;
    maxWidthCss?: string;
  }) => (
    <header data-testid="header-bar" data-label={label} style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}>
      {children}
    </header>
  ),
}));

vi.mock("@/components/timeline", () => ({
  TimelineView: ({ turnActive, maxWidthCss }: { turnActive?: boolean; maxWidthCss?: string }) => (
    <div
      data-testid="timeline"
      data-turn-active={turnActive ? "1" : "0"}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
    />
  ),
}));

vi.mock("@/components/composer", () => {
  function ComposerMock({
    mode,
    onSend,
    onUploadFiles,
    onAttachImage,
    children,
    details,
    maxWidthCss,
  }: {
    mode: string;
    onSend: (s: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>;
    onUploadFiles?: (files: File[]) => void;
    onAttachImage?: (file: File) => Promise<string>;
    children?: React.ReactNode;
    details?: React.ReactNode;
    maxWidthCss?: string;
  }) {
    const [thumbnail, setThumbnail] = useState<string | null>(null);
    const pasteImage = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const image = Array.from(event.clipboardData.files).find((file) =>
        file.type.startsWith("image/"),
      );
      if (!image) return;
      event.preventDefault();
      if (onAttachImage) void onAttachImage(image).then(setThumbnail);
      else onUploadFiles?.([image]);
    };
    return (
      <div
        data-testid="composer"
        data-mode={mode}
        data-max-width={maxWidthCss ?? "undefined"}
      >
        <textarea data-testid="composer-paste-target" onPaste={pasteImage} />
        {thumbnail ? <div data-testid="composer-image-thumbnail" data-url={thumbnail} /> : null}
        <button data-testid="composer-send" onClick={() => void onSend("hi", [], "test-client-message-id")}>send</button>
        <button data-testid="composer-send-multi" onClick={() => void onSend("/compact /update", [], "test-client-message-id")}>send multi</button>
        <button data-testid="composer-send-multi-args" onClick={() => void onSend("/search hello world /compact", [], "test-client-message-id")}>send multi args</button>
        <button data-testid="composer-send-plain" onClick={() => void onSend("plain text", [], "test-client-message-id")}>send plain</button>
        <button data-testid="composer-send-multi-image" onClick={() => void onSend("/compact /update", ["/api/agents/5/uploads/a.png"], "test-client-message-id")}>send multi image</button>
        {details}
        {children}
      </div>
    );
  }
  return { Composer: ComposerMock };
});

vi.mock("@/components/upload-button", () => ({
  UploadButton: ({ agentId }: { agentId: number | null }) => (
    <div data-testid="upload-button" data-agent-id={agentId ?? "null"} />
  ),
}));

vi.mock("@/components/content-toggle", () => ({
  ContentToggle: () => <div data-testid="content-toggle" />,
}));

vi.mock("@/components/inspector-panel", () => ({
  InspectorPanel: () => <div data-testid="inspector-panel" />,
  InspectorToggle: () => <button data-testid="inspector-toggle">toggle</button>,
}));

import HomePage from "./page";

afterEach(cleanup);

function makeAgent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 1,
    spawner: "user",
    fork_source_agent_id: null,
    status: "idling",
    pid: 100,
    spawned_at: "2026-05-15T00:00:00Z",
    started_at: "2026-05-15T00:00:00Z",
    last_active_at: "2026-05-15T00:00:00Z", last_inbound_at: "2026-05-15T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...overrides,
  };
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  hooksState.agents = [];
  hooksState.activeId = null;
  hooksState.forkPending = false;
  hooksState.toast = null;
  hooksState.timelineItems = [];
  hooksState.connectionState = "open";
  hooksState.turnActive = false;
  hooksState.settings = { "display.timeline_width_ratio": 0.4 };
  hooksState.setSetting = vi.fn();
  hooksState.setMobileInspectorOpen = vi.fn();
  hooksState.setInspectorHours = vi.fn();
  hooksState.isLarge = true;
  hooksState.isDesktop = true;
});

describe("HomePage top-level render", () => {
  it("activeId=null → composer 'disabled' + label '…'", () => {
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("disabled");
    expect(screen.getByTestId("header-bar").getAttribute("data-label")).toBe("…");
  });

  it("activeId not null + agent in list → label contains #N + status", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, label: "research", status: "idling" }),
    ];
    wrap(<HomePage />);
    const label = screen.getByTestId("header-bar").getAttribute("data-label");
    expect(label).toContain("Agent #5");
    expect(label).toContain("research");
    expect(label).toContain("Idling");
  });

  it("toast is NOT rendered by the page (the renderer moved to the root ToastHost in Providers, Task #1051)", () => {
    hooksState.toast = "Save failed: 500";
    wrap(<HomePage />);
    expect(screen.queryByText("Save failed: 500")).toBeNull();
  });

  // Layout regression (task #714 + #715): the timeline+composer content
  // column is width-capped and centered (mx-auto), so on wide screens the
  // conversation leaves responsive gutters on both sides — Claude/Gemini
  // style — instead of stretching full-width. The cap comes from the
  // display.timeline_width_ratio user setting as an inline maxWidth
  // (min(<ratio>vw, 1280px)).
  // User ruling 2026-08-06: the timeline is the MAIN page — the scroll
  // surface is full-bleed (scrollbar at the pane edge, gutters scrollable)
  // and HeaderBar + the composer stack float above it (absolute + backdrop
  // blur). The width cap now lives on the timeline's inner content column,
  // the HeaderBar's inner wrapper, and the composer stack's inner wrappers
  // (all mx-auto). jsdom can't measure layout, so assert the class +
  // inline-style contract on the timeline surface and the floating bars.
  it("timeline surface is full-bleed; content column capped and centered", () => {
    wrap(<HomePage />);
    const surface = screen.getByTestId("timeline-surface");
    expect(surface.className).toContain("relative");
    expect(surface.className).toContain(FLEX_1);
    expect(surface.className).toContain(MIN_H_0);
    // The surface must BE a flex container (display:flex + column) — flex-1
    // alone only sets flex-grow/shrink/basis, and without display:flex the
    // height chain breaks: the ScrollArea grows to content height, the
    // viewport has nothing to scroll, the timeline freezes (Task #874, the
    // v0.1.30 mobile-scroll regression).
    expect(surface.className.split(" ")).toContain(FLEX);
    expect(surface.className.split(" ")).toContain(FLEX_COL);
    // min-w-0: the surface is a flex item of the page section. Without it,
    // min-width:auto resolves to the composer footer's min-content (~700px),
    // so on a mobile viewport the surface lays out wider than its
    // overflow-hidden section and the right side is clipped with no way to
    // scroll to it (Task #979 — the v0.1.39 mobile right-truncation report).
    expect(surface.className.split(" ")).toContain(MIN_W_0);
    // Timeline renders inside the surface, not in a capped column.
    expect(surface.contains(screen.getByTestId("timeline"))).toBe(true);
    // Timeline content column: capped + centered inside the scroll viewport.
    const timeline = screen.getByTestId("timeline");
    expect(timeline.style.maxWidth).toBe("min(40vw, 1280px)");
    // Floating bars sit on top of the surface as direct children (their
    // absolute + backdrop-blur classes are asserted in header-bar.test.tsx).
    const header = screen.getByTestId("header-bar");
    expect(header.parentElement).toBe(surface);
    const composer = screen.getByTestId("composer");
    expect(surface.contains(composer)).toBe(true);
  });

  // Task #715: the column cap follows the display.timeline_width_ratio
  // setting (viewport fraction, clamped by lib/timeline-width.ts).
  // R4 (Task #1024): the jsdom class-contract layer covers every timeline
  // invariant in LAYOUT_INVARIANTS (the shared checklist with the
  // Playwright layer). The timeline-surface classes asserted above are the
  // raw material of I1 (no page scroll), I2 (surface not wider than parent),
  // I3 (composer fits) and I6 (single scroll region) — this guard fails when
  // someone adds an invariant to lib/layout.ts without a jsdom contract for
  // the timeline page.
  it("jsdom contract covers every timeline invariant in LAYOUT_INVARIANTS", () => {
    const timelineInvariants = LAYOUT_INVARIANTS.filter((i) => i.pages.includes("timeline"));
    expect(timelineInvariants.map((i) => i.id).sort()).toEqual(["I1", "I2", "I3", "I6"]);
    // The contract classes are the six primitives — the surface carries all of them.
    wrap(<HomePage />);
    const surface = screen.getByTestId("timeline-surface");
    const classes = surface.className.split(" ");
    for (const c of [FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0]) {
      expect(classes).toContain(c);
    }
    // Three viewport tiers are the engine-test contract (asserted for real in
    // tests/e2e/test_layout_invariants.py); keep them pinned here so the
    // shared checklist cannot drift.
    expect(LAYOUT_VIEWPORT_TIERS).toEqual([320, 390, 768]);
  });

  it("content column maxWidth follows display.timeline_width_ratio", () => {
    hooksState.settings = { "display.timeline_width_ratio": 0.6 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("min(60vw, 1280px)");
  });

  // Task #805 (user ruling): the width ratio is a DESKTOP concept. On narrow
  // viewports (<768px) the timeline content column renders FULL-WIDTH — no
  // inline maxWidth at all — even when a ratio is stored in settings. The
  // composer gets the same treatment via a falsy maxWidthCss (asserted in
  // composer.test.tsx).
  it("narrow viewport → content column full-width (no maxWidth, ratio ignored)", () => {
    hooksState.isDesktop = false;
    hooksState.settings = { "display.timeline_width_ratio": 0.2 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("");
    // One falsy convention (audit C2): the composer also gets undefined on
    // narrow viewports — never "" — so a `!= null` check can't cap mobile.
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe("undefined");
    // Floating bars stay direct children of the surface on narrow viewports.
    expect(screen.getByTestId("header-bar").parentElement).toBe(
      screen.getByTestId("timeline-surface"),
    );
  });

  it("desktop viewport → content column keeps the ratio cap", () => {
    hooksState.isDesktop = true;
    hooksState.settings = { "display.timeline_width_ratio": 0.6 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("min(60vw, 1280px)");
    // The composer rides the same cap as the timeline column (#723-⑧).
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe(
      "min(60vw, 1280px)",
    );
  });

  // User ruling 2026-08-23: desktop is a side panel and mobile is a
  // full-screen overlay. Both start closed and open only via the header's
  // inspector toggle — mount never touches either open-state source.
  it("mount does not auto-open the inspector", () => {
    hooksState.isLarge = false;
    hooksState.settings = { "display.inspector_open": false };
    wrap(<HomePage />);
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    expect(hooksState.setMobileInspectorOpen).not.toHaveBeenCalled();
  });

  it("mobile mount with the setting open still does not open the overlay (task #793)", () => {
    hooksState.isLarge = false;
    hooksState.settings = { "display.inspector_open": true };
    wrap(<HomePage />);
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    expect(hooksState.setMobileInspectorOpen).not.toHaveBeenCalled();
  });

  it("renders an open inspector beside HomeContent in the desktop resizable group", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    hooksState.settings = {
      "display.timeline_width_ratio": 0.4,
      "display.inspector_open": true,
    };
    wrap(<HomePage />);

    const main = screen.getByRole("main");
    const homeContent = screen.getByTestId("timeline-surface").closest("section");
    const panel = screen.getByTestId("inspector-panel");
    const timelinePanel = homeContent?.parentElement;
    const inspectorPanel = panel.parentElement;
    const inspectorGroup = timelinePanel?.parentElement;
    expect(inspectorGroup).toBeTruthy();
    expect(main.contains(inspectorGroup!)).toBe(true);
    expect(inspectorPanel?.parentElement).toBe(inspectorGroup);
    expect(inspectorPanel?.previousElementSibling?.getAttribute("role")).toBe("separator");
    const toggle = screen.getByTestId("inspector-toggle");
    expect(screen.getByRole("banner").contains(toggle)).toBe(true);
    expect(screen.getByTestId("composer").contains(toggle)).toBe(false);
  });
});

describe("composerMode derivation", () => {
  it("activeId not null + status='terminated' → idle (send auto-resurrects)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "terminated" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });

  it("activeId + turnActive=true → busy", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = true;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });

  it("activeId + not terminated + turnActive=false → idle", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "idling" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });

  it("status='running' + turnActive=false → busy (Stop available between actions)", () => {
    // The durable cancel is caught at claim even between actions, so Stop is
    // offered for the whole running state, not only mid-turn (turnActive).
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });

  // Regression (streaming jitter): raw turnActive flips false at every
  // MID-turn llm_done (one per LLM call, not per turn), which used to make
  // the timeline's last detail block collapse + re-expand at each call
  // boundary. TimelineView must receive the steady busy union
  // (turnActive || status==='running'), same signal as the Stop button.
  it("TimelineView receives busy=true while status='running' even when turnActive=false", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "running" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("1");
  });

  it("TimelineView receives busy=false when idle and no turn in flight", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("0");
  });
});

describe("Spawn flow", () => {
  it("spawn succeeds → focusComposer is called", async () => {
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.focusComposer).toHaveBeenCalled());
  });

  it("spawn returns null → focusComposer not called", async () => {
    hooksState.spawn = vi.fn().mockResolvedValue(null);
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.spawn).toHaveBeenCalled());
    expect(hooksState.focusComposer).not.toHaveBeenCalled();
  });
});

// Force-scroll on send: page.tsx calls the store's requestScrollToBottom
// action (the switch-side bump lives in the store's switchThread, not
// here). The store is mocked, so we assert the action fires.
describe("force-scroll on send", () => {
  it("send succeeds → requestScrollToBottom called (sending scrolls to bottom)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() =>
      expect(vi.mocked(api.sendMessage)).toHaveBeenCalledWith(5, "hi", expect.any(String)),
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).toHaveBeenCalledTimes(1);
  });

  it("send fails → requestScrollToBottom NOT called (no scroll on error)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    vi.mocked(api.sendMessage).mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).not.toHaveBeenCalled();
  });
});

describe("UploadButton receives activeId", () => {
  it("activeId=null → upload-button data-agent-id 'null'", () => {
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("null");
  });

  it("activeId=7 → upload-button data-agent-id '7'", () => {
    hooksState.activeId = 7;
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("7");
  });
});

describe("pasted image routing", () => {
  const uploadResult = {
    files: [
      {
        filename: "paste.png",
        path: "/tmp/paste.png",
        url: "/api/agents/5/uploads/paste.png",
        size: 3,
        content_type: "image/png",
      },
    ],
  };

  it("uses file delivery without a native thumbnail for a text-only agent", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, supports_vision: false })];
    vi.mocked(api.uploadFiles).mockResolvedValueOnce(uploadResult);
    const image = new File(["png"], "paste.png", { type: "image/png" });

    wrap(<HomePage />);
    fireEvent.paste(screen.getByTestId("composer-paste-target"), {
      clipboardData: { files: [image], types: ["Files"] },
    });

    await waitFor(() => expect(vi.mocked(api.uploadFiles)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.uploadFiles)).toHaveBeenCalledWith(
      5,
      [image],
      expect.any(Function),
    );
    expect(vi.mocked(api.uploadFiles)).not.toHaveBeenCalledWith(5, [image], undefined, false);
    expect(screen.queryByTestId("composer-image-thumbnail")).toBeNull();
  });

  it("keeps the native image attachment path for a vision-capable agent", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, supports_vision: true })];
    vi.mocked(api.uploadFiles).mockResolvedValueOnce(uploadResult);
    const image = new File(["png"], "paste.png", { type: "image/png" });

    wrap(<HomePage />);
    fireEvent.paste(screen.getByTestId("composer-paste-target"), {
      clipboardData: { files: [image], types: ["Files"] },
    });

    await waitFor(() =>
      expect(vi.mocked(api.uploadFiles)).toHaveBeenCalledWith(5, [image], undefined, false),
    );
    expect(await screen.findByTestId("composer-image-thumbnail")).toBeTruthy();
  });
});

// ── Multi-command dispatch ──
// One send is one message. Text invoking several commands goes to the agent
// whole; expanding it into the individual commands is the backend's job
// (ava._commands.expand_command), inside that single inbound. Splitting here
// would make the agent claim each command as its own turn, blind to the rest.

describe("multi-command dispatch", () => {
  it("sends one message for two space-separated commands", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.sendMessage)).toHaveBeenCalledWith(
      5,
      "/compact /update",
      expect.any(String),
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).toHaveBeenCalledTimes(1);
  });

  it("sends command arguments verbatim in that one message", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi-args"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.sendMessage)).toHaveBeenCalledWith(
      5,
      "/search hello world /compact",
      expect.any(String),
    );
  });

  it("sends a single message for plain text (no slash prefix)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-plain"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.sendMessage)).toHaveBeenCalledWith(
      5,
      "plain text",
      expect.any(String),
    );
  });

  it("attaches images to the one command message", async () => {
    // Attachments have nowhere else to go now: there is a single message, so
    // the "which segment carries the image" question does not arise.
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi-image"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.sendMessage)).toHaveBeenCalledWith(
      5,
      [
        { type: "text", text: "/compact /update" },
        { type: "image_url", image_url: { url: "/api/agents/5/uploads/a.png" } },
      ],
      expect.any(String),
    );
  });

  it("does not scroll when the send fails", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    vi.mocked(api.sendMessage).mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(vi.mocked(api.sendMessage)).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).not.toHaveBeenCalled();
  });
});
