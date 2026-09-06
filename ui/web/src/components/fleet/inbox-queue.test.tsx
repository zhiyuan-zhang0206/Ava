import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, NoticeItem, OpenNotice, PageRow, TaskSummaryRow } from "@/lib/types";
import type { NoticesFeed } from "@/lib/use-notices";

// Echo the timestamp back so a test can tell WHICH timestamp a row rendered
// (created_at vs resolved_at) rather than only that some time was shown.
vi.mock("@/lib/sidebar", () => ({ formatRelativeTime: (ts: string) => `rel:${ts}` }));

// api.resolveNotice is the only network call (the shared OpenNoticeDetail makes
// it). Mocked.
const resolveNotice = vi.fn<
  (agentId: number, noticeId: number, body: { action: string; reply?: string }) => Promise<{ status: string }>
>();
vi.mock("@/lib/api", () => ({
  api: {
    resolveNotice: (a: number, n: number, b: { action: string; reply?: string }) => resolveNotice(a, n, b),
  },
}));

// The whole queue is injected through the single data contract
// (open/awaiting + resolved history). Default: empty.
const inboxFeedMock = vi.fn<() => NoticesFeed>();
// The instant local drop after a resolve (Task #1814) — mocked so a click
// under test never needs a real query cache.
const dropOpenNotices = vi.fn<(queryClient: unknown, noticeIds: number[]) => void>();
vi.mock("@/lib/use-notices", () => ({
  useNotices: () => inboxFeedMock(),
  dropOpenNotices: (qc: unknown, ids: number[]) => dropOpenNotices(qc, ids),
}));

// The queue joins entries to the task registry client-side (grouping); feed it a
// controlled task list. Default: none — every entry stays a flat row.
const useTasksMock = vi.fn<(window?: string, fields?: string) => { tasks: TaskSummaryRow[]; loading: boolean; error: boolean }>(
  () => ({ tasks: [], loading: false, error: false }),
);
vi.mock("@/lib/use-tasks", () => ({
  useTasks: (window?: string, fields?: string) => useTasksMock(window, fields),
}));

// The fleet-wide open-pages feed drives the "agent's live page" affordances.
// Stub the hook (default: no pages).
const useAllPagesMock = vi.fn<() => PageRow[]>(() => []);
vi.mock("@/lib/use-all-pages", () => ({ useAllPages: () => useAllPagesMock() }));

import { InboxQueue } from "./inbox-queue";

const scrollIntoViewMock = vi.fn();
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: scrollIntoViewMock,
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  inboxFeedMock.mockReturnValue(emptyFeed());
  useTasksMock.mockReturnValue({ tasks: [], loading: false, error: false });
  useAllPagesMock.mockReturnValue([]);
});

function emptyFeed(over: Partial<NoticesFeed> = {}): NoticesFeed {
  return {
    open: over.open ?? [],
    awaiting: over.awaiting ?? [],
    resolved: over.resolved ?? [],
    fetchNextPage: over.fetchNextPage ?? vi.fn(),
    hasNextPage: over.hasNextPage ?? false,
    isFetchingNextPage: over.isFetchingNextPage ?? false,
    error: over.error ?? false,
    resolvedError: over.resolvedError ?? false,
    isLoading: over.isLoading ?? false,
  };
}
function setFeed(over: Partial<NoticesFeed> = {}) {
  inboxFeedMock.mockReturnValue(emptyFeed(over));
}

// require_response open notice (rides the agent snapshot).
// A require_response notice — the awaiting half of the feed (NoticeItem
// shape, as the unified endpoint returns it).
function n(over: Partial<NoticeItem> & { id: number; title: string }): NoticeItem {
  return {
    id: over.id,
    agent_id: over.agent_id ?? 7,
    agent_label: over.agent_label ?? "worker",
    title: over.title,
    content: over.content ?? null,
    priority: over.priority ?? "P2",
    require_response: over.require_response ?? true,
    blocking: over.blocking ?? false,
    created_at: over.created_at ?? "2026-06-14T00:00:00Z",
    updated_at: over.updated_at ?? null,
    resolved_at: null,
    resolution: null,
    reply: null,
    task_id: over.task_id ?? null,
  };
}

// Standalone NoticeItem (FYI feed / resolved history).
function ni(over: Partial<NoticeItem> & { id: number; title: string }): NoticeItem {
  return {
    id: over.id,
    agent_id: over.agent_id ?? 5,
    agent_label: over.agent_label ?? "worker",
    title: over.title,
    content: over.content ?? null,
    priority: over.priority ?? "P2",
    require_response: over.require_response ?? false,
    blocking: over.blocking ?? false,
    created_at: over.created_at ?? "2026-06-17T00:00:00Z",
    resolved_at: over.resolved_at ?? null,
    resolution: over.resolution ?? null,
    reply: over.reply ?? null,
  };
}

function mkPage(over: Partial<PageRow> & { agent_id: number; name: string }): PageRow {
  return {
    id: 1,
    port: 10000,
    title: over.name,
    serve_dir: null,
    url: `http://host/${over.name}`,
    created_at: "2026-01-01T00:00:00Z",
    closed_at: null,
    ...over,
  };
}

function agent(over: {
  agent_id: number;
  label?: string;
  status?: AgentRow["status"];
  notices_awaiting_response?: OpenNotice[];
}): AgentRow {
  return {
    agent_id: over.agent_id,
    spawner: "user",
    fork_source_agent_id: null,
    status: over.status ?? "idling",
    pid: null,
    spawned_at: "2026-06-14T00:00:00Z",
    started_at: null,
    last_active_at: "2026-06-14T00:00:00Z", last_inbound_at: "2026-06-14T00:00:00Z",
    label: over.label ?? null,
    machine: "test",
    supports_vision: true,
    notices_awaiting_response: over.notices_awaiting_response ?? [],
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
  };
}

function renderQueue(
  agents: AgentRow[],
  props: Omit<ComponentProps<typeof InboxQueue>, "agents"> = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InboxQueue agents={agents} {...props} />
    </QueryClientProvider>,
  );
}

// The title list (master column) — scope queries here so an auto-selected notice's
// detail heading doesn't double-count its title.
function listTitles(): string[] {
  const list = screen.getByRole("list", { name: /inbox queue/i });
  return within(list)
    .getAllByRole("button")
    .map((b) => b.querySelector("span.truncate")?.textContent ?? "")
    .filter((t) => t !== "");
}

describe("InboxQueue — open stream", () => {
  it("shows an empty state when nothing is open and nothing resolved", () => {
    setFeed();
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText(/Nothing needs your attention/)).not.toBeNull();
  });

  it("merges require_response + FYI into ONE stream, sorted P0->P3 then blocking then oldest", () => {
    setFeed({
      open: [ni({ id: 20, title: "fyi-p0", priority: "P0" }), ni({ id: 21, title: "fyi-p3", priority: "P3" })],
      awaiting: [n({ id: 10, title: "dec-p2", priority: "P2" })],
    });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    // One list interleaving both kinds by priority.
    expect(listTitles()).toEqual(["fyi-p0", "dec-p2", "fyi-p3"]);
  });

  it("breaks priority ties by blocking, then oldest", () => {
    setFeed({
      awaiting: [
        n({ id: 30, title: "t-newer", priority: "P2", created_at: "2026-06-14T02:00:00Z" }),
        n({ id: 31, title: "t-older", priority: "P2", created_at: "2026-06-14T01:00:00Z" }),
        n({ id: 32, title: "t-block", priority: "P2", blocking: true, created_at: "2026-06-14T03:00:00Z" }),
      ],
    });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    expect(listTitles()).toEqual(["t-block", "t-older", "t-newer"]);
  });
});

describe("InboxQueue — route anchor", () => {
  it("scrolls to and briefly highlights the matching notice without reordering", async () => {
    vi.useFakeTimers();
    const raf = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        callback(0);
        return 1;
      });
    try {
      setFeed({
        awaiting: [
          n({ id: 1, agent_id: 1, title: "first", priority: "P0" }),
          n({ id: 2, agent_id: 405, title: "target", priority: "P3" }),
        ],
      });
      renderQueue(
        [agent({ agent_id: 1 }), agent({ agent_id: 405 })],
        { anchorAgentId: 405 },
      );

      expect(listTitles()).toEqual(["first", "target"]);
      const target = screen.getByText("target").closest('[data-testid="inbox-row"]');
      expect(target?.getAttribute("data-anchor-highlighted")).toBe("true");
      expect(target?.className).toContain("bg-primary/10");
      expect(scrollIntoViewMock).toHaveBeenCalledWith({ block: "nearest" });

      await act(() => vi.advanceTimersByTime(1_600));
      expect(target?.getAttribute("data-anchor-highlighted")).toBeNull();
      expect(listTitles()).toEqual(["first", "target"]);
    } finally {
      raf.mockRestore();
      vi.useRealTimers();
    }
  });

  it("keeps the default position when that agent has no notice", () => {
    setFeed({
      awaiting: [
        n({ id: 1, agent_id: 1, title: "first", priority: "P0" }),
        n({ id: 2, agent_id: 2, title: "second", priority: "P3" }),
      ],
    });
    renderQueue(
      [agent({ agent_id: 1 }), agent({ agent_id: 2 })],
      { anchorAgentId: 405 },
    );

    expect(listTitles()).toEqual(["first", "second"]);
    expect(screen.getByRole("heading", { name: "first" })).toBeTruthy();
    expect(document.querySelector('[data-anchor-highlighted="true"]')).toBeNull();
    expect(scrollIntoViewMock).not.toHaveBeenCalled();
  });
});

describe("InboxQueue — decision (require_response) action surface", () => {
  it("auto-selects the top notice; submits an answer via the Send button", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Send it?", content: "A) yes\nB) no", priority: "P0" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    // Top notice auto-selected -> its content shows without a click.
    expect(screen.getByText(/A\) yes/)).not.toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "A" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "answer", reply: "A" }));
  });

  it("Enter submits; Shift+Enter does not; IME composition does not", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "yes" } });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(resolveNotice).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(resolveNotice).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "answer", reply: "yes" }));
  });

  it("does not submit a blank answer", () => {
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.click(screen.getByLabelText("Send answer"));
    expect(resolveNotice).not.toHaveBeenCalled();
  });

  it("dismiss submits action:'dismiss' with no reply", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.click(screen.getByText("Dismiss"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "dismiss" }));
  });

  it("auto-advances to the next open notice after a send", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({
      awaiting: [n({ id: 1, title: "first", priority: "P0" }), n({ id: 2, title: "second", priority: "P1" })],
    });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    // "first" auto-selected.
    expect(screen.getByLabelText(/Reply to notice: first/)).not.toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a1" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 1, { action: "answer", reply: "a1" }));
    // Selection advances -> the next notice's reply box mounts.
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: second/)).not.toBeNull());
  });

  it("empty-box Down/Up arrows cycle the selection", async () => {
    setFeed({
      awaiting: [n({ id: 1, title: "first", priority: "P0" }), n({ id: 2, title: "second", priority: "P1" })],
    });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: second/)).not.toBeNull());
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "ArrowUp" });
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: first/)).not.toBeNull());
  });
});

describe("InboxQueue — FYI action surface", () => {
  it("a top FYI notice shows Mark read and NO Send button", () => {
    setFeed({ open: [ni({ id: 1, title: "migration done" })] });
    renderQueue([]);
    expect(screen.getByRole("heading", { name: "migration done" })).toBeTruthy();
    expect(screen.getByText("Mark read")).toBeTruthy();
    expect(screen.queryByLabelText("Send answer")).toBeNull();
  });

  it("Mark read calls resolveNotice with action:'read' and no reply", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ open: [ni({ id: 1, agent_id: 7, title: "fyi" })] });
    renderQueue([]);
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 1, { action: "read" }));
  });

  it("Enter in the note box sends the reply and marks read", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ open: [ni({ id: 3, agent_id: 9, title: "decision" })] });
    renderQueue([]);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "go with A" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(9, 3, { action: "read", reply: "go with A" }));
  });

  it("surfaces a 409 as already-read", async () => {
    resolveNotice.mockRejectedValue(new Error("409 conflict"));
    setFeed({ open: [ni({ id: 5, agent_id: 1, title: "stale" })] });
    renderQueue([]);
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(screen.getByText(/already read/i)).toBeTruthy());
  });


  it("drops the resolved notice from the open queue immediately", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({
      open: [
        ni({ id: 1, agent_id: 7, title: "first" }),
        ni({ id: 2, agent_id: 7, title: "second" }),
      ],
    });
    renderQueue([]);
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(dropOpenNotices).toHaveBeenCalledWith(expect.anything(), [1]));
  });
});

describe("InboxQueue — resolved history (collapsed disclosure)", () => {
  it("keeps the resolved history behind a collapsed disclosure, off the main stream", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice", resolution: "answered", reply: "my recorded answer" })] });
    renderQueue([agent({ agent_id: 1 })]);
    // Collapsed: the row is not in the stream until the disclosure is opened.
    expect(screen.queryByText("old notice")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("old notice"));
    expect(screen.getByText(/my recorded answer/)).not.toBeNull();
    // The resolved detail has no reply box.
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("shows the word 'Resolved' without a count badge (user ruling 2026-08-09 #1096)", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice" }), ni({ id: 51, title: "older notice" })] });
    renderQueue([agent({ agent_id: 1 })]);
    const btn = screen.getByRole("button", { name: "Resolved" });
    // Accessible name is exactly the word — no trailing count (old code:
    // the button read "Resolved 2").
    expect(btn.getAttribute("aria-label") ?? btn.textContent).toBe("Resolved");
    expect(btn.textContent).not.toMatch(/\d/);
  });

  // A resolved row is stamped like an open one — agent label + the notice's own
  // creation time. It previously rendered the RESOLUTION time in place of both
  // (the label included), so the row lost its author and read as newer than the
  // question actually was.
  it("stamps a resolved row with its agent label and creation time, not its resolution time", () => {
    setFeed({
      resolved: [
        ni({
          id: 50,
          title: "old notice",
          agent_label: "worker",
          created_at: "2026-06-17T00:00:00Z",
          resolved_at: "2026-06-18T09:30:00Z",
          resolution: "answered",
        }),
      ],
    });
    renderQueue([agent({ agent_id: 1 })]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const row = within(list).getByText("old notice").closest("button");
    expect(row?.textContent).toContain("worker");
    expect(row?.textContent).toContain("rel:2026-06-17T00:00:00Z");
    expect(row?.textContent).not.toContain("rel:2026-06-18T09:30:00Z");
  });

  it("keeps the resolution inline with the agent's text, not pinned to the panel bottom", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice", content: "agent said this", resolution: "answered", reply: "my recorded answer" })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("old notice"));

    // The reply lives INSIDE the scrolling body that holds the agent's text —
    // as a bottom-pinned footer it sat outside that scroller entirely.
    const scroller = screen.getByText(/my recorded answer/).closest(".overflow-y-auto");
    expect(scroller).not.toBeNull();
    expect(scroller!.contains(screen.getByText("agent said this"))).toBe(true);
  });

  it("labels a dismissed resolution", () => {
    setFeed({ resolved: [ni({ id: 51, title: "skipped", require_response: true, resolution: "dismissed", reply: null })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("skipped"));
    expect(screen.getByText(/Dismissed/)).not.toBeNull();
  });

  it("pages back with Show more once the disclosure is open", () => {
    const fetchNextPage = vi.fn();
    setFeed({ resolved: [ni({ id: 60, title: "r1" })], hasNextPage: true, fetchNextPage });
    renderQueue([]);
    // Show more is hidden while the disclosure is collapsed.
    expect(screen.queryByRole("button", { name: /Show more/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByRole("button", { name: /Show more/ }));
    expect(fetchNextPage).toHaveBeenCalled();
  });
});

// ── Task-subtree grouping (unchanged join; entries can be either kind) ──

function tk(id: number, over: Partial<TaskSummaryRow> = {}): TaskSummaryRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    reminder_count: 0,
    ...over,
  };
}

describe("InboxQueue — task grouping", () => {
  // Registry: root #1 → manager #2 → worker #3; agents 30/40 sit in the manager
  // subtree, agent 999 owns no task.
  function withRegistry() {
    useTasksMock.mockReturnValue({
      tasks: [
        tk(1, { title: "root" }),
        tk(2, { parent_id: 1, title: "ship feature", owner: 30 }),
        tk(3, { parent_id: 2, title: "write tests", owner: 40 }),
      ],
      loading: false,
      error: false,
    });
  }

  it("bounds task grouping to the seven-day registry window", () => {
    withRegistry();
    renderQueue([]);
    expect(useTasksMock).toHaveBeenCalledWith("7d", "summary");
  });

  it("groups entries under the top-level task subtree with a count", () => {
    withRegistry();
    setFeed({
      awaiting: [
        n({ id: 1, title: "q-mgr", priority: "P2", agent_id: 30 }),
        n({ id: 2, title: "q-wrk", priority: "P3", agent_id: 40 }),
      ],
    });
    renderQueue([agent({ agent_id: 30, label: "mgr" }), agent({ agent_id: 40, label: "wrk" })]);
    const header = screen.getByLabelText("Task #2: ship feature");
    expect(header.textContent).toContain("#2 ship feature");
    expect(header.textContent).toContain("2");
    expect(listTitles()).toEqual(["q-mgr", "q-wrk"]);
  });

  it("pulls a subtree together at its best-ranked entry; loose agents stay flat", () => {
    withRegistry();
    setFeed({
      awaiting: [
        n({ id: 5, title: "q-loose", priority: "P1", agent_id: 999 }),
        n({ id: 6, title: "q-mgr", priority: "P2", agent_id: 30 }),
        n({ id: 7, title: "q-wrk", priority: "P0", agent_id: 40 }),
      ],
    });
    renderQueue([
      agent({ agent_id: 999, label: "loose" }),
      agent({ agent_id: 30, label: "mgr" }),
      agent({ agent_id: 40, label: "wrk" }),
    ]);
    // Sorted flat: q-wrk (P0), q-loose (P1), q-mgr (P2). The subtree group is
    // anchored where q-wrk sat, so its members render before the loose P1.
    expect(listTitles()).toEqual(["q-wrk", "q-mgr", "q-loose"]);
    expect(screen.getByLabelText("Task #2: ship feature")).not.toBeNull();
  });

  it("renders no group headers when the registry is empty", () => {
    setFeed({ awaiting: [n({ id: 1, title: "q1", agent_id: 30 })] });
    renderQueue([agent({ agent_id: 30, label: "mgr" })]);
    expect(screen.queryByLabelText(/^Task #/)).toBeNull();
  });
});

describe("InboxQueue — error presentation (audit C3)", () => {
  // Stale-while-error: a failed fetch keeps its last data on screen and the
  // failure is surfaced, so "queue empty" is never indistinguishable from
  // "couldn't load" (previously both hooks failed silently to an empty list).
  it("open feed failure with nothing loaded → failure screen, not the empty state", () => {
    setFeed({ error: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
    expect(screen.queryByText(/Nothing needs your attention/)).toBeNull();
  });

  it("open feed failure with notices on screen → Stale badge, entries still shown", () => {
    setFeed({ error: true, awaiting: [n({ id: 10, title: "dec-p2" })] });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    expect(screen.getByText("Stale")).not.toBeNull();
    expect(listTitles()).toEqual(["dec-p2"]);
  });

  it("resolved-history failure with open entries on screen → unavailable line", () => {
    setFeed({ resolvedError: true, open: [ni({ id: 21, title: "fyi" })] });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Resolved history unavailable.")).not.toBeNull();
    expect(listTitles()).toEqual(["fyi"]);
  });

  it("any cold failure with nothing at all loaded → failure screen", () => {
    setFeed({ resolvedError: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
  });

  it("first load in flight → Loading, not the empty state", () => {
    setFeed({ isLoading: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Loading…")).not.toBeNull();
    expect(screen.queryByText(/Nothing needs your attention/)).toBeNull();
  });
});

describe("InboxQueue — terminated agent", () => {
  // The inbox carries no resurrect affordance: replying to a terminated agent
  // resurrects it on its own, and the sidebar row keeps an explicit button for
  // the deliberate case. A second one here only invited a dead-end click.
  it("offers no resurrect button for a terminated agent's notice", () => {
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "dead", status: "terminated" })]);
    // The notice itself is still selectable and answerable.
    expect(screen.getAllByText("Q?").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /Resurrect Agent #7/ })).toBeNull();
  });
});

describe("InboxQueue — agent live-page entry", () => {
  it("a queue row links to the agent's live page (fleet-wide, no per-row fetch)", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const link = within(list).getByRole("link", { name: /open live page/i });
    expect(link.getAttribute("href")).toBe("http://host/dash");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("no page link on a row whose agent has no open page", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 2, name: "other" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    expect(within(list).queryByRole("link", { name: /open live page/i })).toBeNull();
  });

  it("the detail header also links the selected agent's live page", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    // Two entries to the same page: the list row + the auto-selected detail header.
    const links = screen.getAllByRole("link", { name: /open live page/i });
    expect(links.length).toBe(2);
    expect(links.every((l) => l.getAttribute("href") === "http://host/dash")).toBe(true);
  });
});
