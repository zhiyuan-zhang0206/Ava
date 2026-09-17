import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentArchive } from "./archive";
import type { InnerProps } from "./types";
import type { AgentRow } from "@/lib/types";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { listAgents: vi.fn() } }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/components/agent-row", () => ({ AgentRow: ({ agent, onSelect }: { agent: AgentRow; onSelect: () => void }) => <li><button onClick={onSelect}>#{agent.agent_id}</button></li> }));
const props = { onSelect: vi.fn(), pendingActions: {}, wide: true } as unknown as InnerProps & { wide: boolean };
const row = (id: number) => ({ agent_id: id }) as AgentRow;
let client: QueryClient;
beforeEach(() => { vi.resetAllMocks(); client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); });
afterEach(() => { cleanup(); client.clear(); });
function mount() { return render(<QueryClientProvider client={client}><AgentArchive props={props} /></QueryClientProvider>); }

describe("bounded agent archive", () => {
  it("keeps one page, releases the previous page, and opens an ID directly", async () => {
    vi.mocked(api.listAgents).mockImplementation(({ beforeId } = {}) => Promise.resolve(beforeId == null ? { agents: [row(10)], next_cursor: 10 } : { agents: [row(9)], next_cursor: null }));
    mount();
    await waitFor(() => expect(screen.getByText("#10")).toBeTruthy());
    fireEvent.click(screen.getByText("archiveOlder"));
    await waitFor(() => expect(screen.getByText("#9")).toBeTruthy());
    expect(screen.queryByText("#10")).toBeNull();
    await waitFor(() => expect(client.getQueryCache().findAll({ queryKey: ["agent-directory"] })).toHaveLength(1));
    fireEvent.click(screen.getByText("#9"));
    expect(props.onSelect).toHaveBeenCalledWith(9);
    expect(api.listAgents).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "terminated", beforeId: 10, limit: 50 }));
  });
  it("changing search cancels the old request and cannot display its late result", async () => {
    let signal: AbortSignal | undefined;
    let finish!: (value: { agents: AgentRow[]; next_cursor: null }) => void;
    vi.mocked(api.listAgents).mockImplementation(({ query, signal: requestSignal } = {}) => {
      if (!query) { signal = requestSignal; return new Promise((resolve) => { finish = resolve; }); }
      return Promise.resolve({ agents: [row(22)], next_cursor: null });
    });
    mount();
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.change(screen.getByLabelText("archiveSearch"), { target: { value: "target" } });
    await waitFor(() => expect(screen.getByText("#22")).toBeTruthy());
    expect(signal?.aborted).toBe(true);
    await act(async () => { finish({ agents: [row(99)], next_cursor: null }); await Promise.resolve(); });
    expect(screen.queryByText("#99")).toBeNull();
  });
});
