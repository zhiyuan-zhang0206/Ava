import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem, TimelineResponse } from "@/lib/contracts/types";
import { saveScrollMemory } from "@/lib/layout/scroll-memory";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { TimelineView } from ".";

vi.mock("@/lib/transport/api", () => ({ api: {
  getTimeline: vi.fn(), getConfig: vi.fn().mockResolvedValue({ fields: [] }),
} }));
vi.mock("@/lib/transport/useEventStream", () => ({ useAgentEventStream: vi.fn() }));
vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({ settings: {}, setSetting: vi.fn(), isLoading: false }),
}));
vi.mock("@/lib/layout/content-toggle-store", () => ({
  useContentToggle: () => ({ detailsMode: "all", isLoading: false }),
  useContentToggleReset: (selector: (state: { resetToken: number }) => unknown) => selector({ resetToken: 0 }),
}));
vi.mock("../content/markdown", () => ({ ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div> }));
// happy-dom does not lay out the scroll area. Keep its DOM contract and model
// browser geometry below; the real useTimeline/store/view effects stay intact.
vi.mock("../ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => (
    <div><div data-testid="history-viewport" data-slot="scroll-area-viewport">{children}</div></div>
  ),
}));

const items: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
  item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
  source: null, created_at: null, inbound_id: null, show_timestamp: false,
}));
const response: TimelineResponse = { items, msg_count: 17, has_more: false };
let top = 0;
let height = 5000;
let client: QueryClient;
let resizeCallbacks: Set<() => void>;

function Conversation({ agentId, entry }: { agentId: number | null; entry: string }) {
  const timeline = useTimeline(agentId, () => undefined);
  return <TimelineView items={timeline.items} threadKey={agentId === null ? undefined : String(agentId)} scrollMemoryKey={entry} />;
}
function conversation(agentId: number | null, entry: string) {
  return <QueryClientProvider client={client}><Conversation agentId={agentId} entry={entry} key={entry} /></QueryClientProvider>;
}
function viewport() { return screen.getByTestId("history-viewport"); }
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["timeline", 42], response);
  client.setQueryData(["timeline", 43], response);
  client.setQueryData(["config", null], { fields: [] });
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
  height = 5000;
  resizeCallbacks = new Set();
  vi.stubGlobal("ResizeObserver", class {
    private readonly deliver: () => void;
    constructor(callback: ResizeObserverCallback) {
      this.deliver = () => callback([], this);
    }
    observe(target: Element) {
      if (target.getAttribute("data-slot") === "scroll-area-viewport") resizeCallbacks.add(this.deliver);
    }
    disconnect() { resizeCallbacks.delete(this.deliver); }
    unobserve() { resizeCallbacks.delete(this.deliver); }
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.querySelectorAll(".timeline-item").length ? height : 600;
  });
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation(function (this: HTMLElement, value) {
    top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const y = this.dataset.itemId ? (Number.parseInt(this.dataset.itemId) - 1) * 300 - top : 0;
    return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
  });
});
afterEach(() => {
  cleanup(); client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("history entry scroll ownership", () => {
  it("restores a returned entry while the real timeline hook seeds an empty live store", () => {
    saveScrollMemory("history-cold-live", { contentKey: "42", scrollTop: 700, followBottom: false });
    render(conversation(42, "history-cold-live"));
    expect(viewport().scrollTop).toBe(700);
  });
  it("restores a returned entry when live items are already measurable on its first commit", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-warm-live", { contentKey: "42", scrollTop: 700, followBottom: false });
    render(conversation(42, "history-warm-live"));
    expect(viewport().scrollTop).toBe(700);
  });
  it("keeps a restored reader parked when content grows", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-growth", { contentKey: "42", scrollTop: 700, followBottom: false });
    render(conversation(42, "history-growth"));
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it.each(["fresh-entry", "returning-follower", "mismatched-content"])("keeps following after %s", (entry) => {
    if (entry !== "fresh-entry") saveScrollMemory(entry, {
      contentKey: entry === "mismatched-content" ? "43" : "42",
      scrollTop: entry === "returning-follower" ? 4400 : 700,
      followBottom: entry === "returning-follower",
    });
    render(conversation(42, entry));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });
  it("restores through a strict-mode effect re-attach", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-strict", { contentKey: "42", scrollTop: 700, followBottom: false });
    render(<StrictMode>{conversation(42, "history-strict")}</StrictMode>);
    expect(viewport().scrollTop).toBe(700);
  });
  it("remembers independent positions across entry unmounts and returns", () => {
    const { rerender } = render(conversation(42, "history-first"));
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-second"));
    act(() => { viewport().scrollTop = 1200; fireEvent.scroll(viewport()); });
    rerender(<QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>);
    top = 0;
    rerender(conversation(42, "history-first"));
    expect(viewport().scrollTop).toBe(700);
    rerender(<QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>);
    top = 0;
    rerender(conversation(43, "history-second"));
    expect(viewport().scrollTop).toBe(1200);
  });
  it("keeps user send and subsequent selection commands after a restore", () => {
    saveScrollMemory("history-send", { contentKey: "42", scrollTop: 700, followBottom: false });
    const { rerender } = render(conversation(42, "history-send"));
    expect(viewport().scrollTop).toBe(700);
    act(() => { useTimelineStore.getState().requestScrollToBottom(); });
    expect(viewport().scrollTop).toBe(4400);
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-send"));
    expect(viewport().scrollTop).toBe(4400);
  });
});
