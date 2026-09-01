// useTasks tests — the Task Graph data source, focused on its resilience
// contract: the board self-heals (keeps polling after an error) and never
// blanks a loaded board on a transient poll failure (stale-while-error).
//
// api.getTasks is mocked; a real QueryClient (retry off) drives the query so the
// success/error edges run end to end. useEventStream is mocked out — the
// spawn/update-driven invalidate is covered by useEventStream's own suite; here
// we only care about the query result derivation.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import type { TaskListResponse, TaskRow } from "./types";
import { useTasks } from "./use-tasks";

vi.mock("./api", () => ({
  api: { getTasks: vi.fn() },
}));

// useEventStream needs a Provider ancestor; mock it to a noop — the query path is
// what these tests exercise.
vi.mock("./useEventStream", () => ({
  useEventStream: vi.fn(),
  EventStreamProvider: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
}));

const getTasks = vi.mocked(api.getTasks);

function task(id: number): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description_preview: "",
    results_preview: null,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    reminder_count: 0,
  };
}

const RESPONSE: TaskListResponse = { tasks: [task(1), task(2)] };

function withClient(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children);
  };
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  getTasks.mockReset();
});

afterEach(cleanup);

describe("useTasks", () => {
  it("while pending: empty list, loading=true, error=false", () => {
    getTasks.mockReturnValue(new Promise<TaskListResponse>(() => {
      /* never settles — keeps the query pending */
    }));

    const { result } = renderHook(() => useTasks(), { wrapper: withClient(freshClient()) });

    expect(result.current.tasks).toEqual([]);
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("on success: returns the tasks, loading=false, error=false", async () => {
    getTasks.mockResolvedValue(RESPONSE);

    const { result } = renderHook(() => useTasks(), { wrapper: withClient(freshClient()) });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);
  });

  it("requests the compact task-list projection", async () => {
    getTasks.mockResolvedValue(RESPONSE);

    const { result } = renderHook(() => useTasks(), { wrapper: withClient(freshClient()) });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(getTasks).toHaveBeenCalledWith({ window: "all", fields: "summary" });
  });

  it("cold error: empty list, loading=false, error=true", async () => {
    getTasks.mockRejectedValue(new Error("HTTP 500"));

    const { result } = renderHook(() => useTasks(), { wrapper: withClient(freshClient()) });

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.tasks).toEqual([]);
  });

  it("stale-while-error: a failed poll after data keeps the last good tasks (error flagged, not blanked)", async () => {
    getTasks.mockResolvedValueOnce(RESPONSE);
    getTasks.mockRejectedValue(new Error("gateway down"));
    const client = freshClient();

    const { result } = renderHook(() => useTasks(), { wrapper: withClient(client) });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);

    // Force the next poll (the mock now rejects).
    await act(async () => {
      await client.refetchQueries({ queryKey: ["tasks"] });
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    // The board is NOT cleared — the last-loaded tasks are still served.
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
  });
});
