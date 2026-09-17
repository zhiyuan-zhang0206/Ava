// ErrorBoundary resetKey tests — the boundary must be resettable on a
// selection change (e.g. an agent switch) WITHOUT remounting healthy
// children: the inspector's per-agent cache lives in the children's query
// observers, so a `key=` remount would discard it (task #3894).

import { fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./error-boundary";

function MountProbe({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount();
  }, [onMount]);
  return <p>probe content</p>;
}

describe("ErrorBoundary resetKey", () => {
  it("changing resetKey while healthy does not remount children", () => {
    let mounts = 0;
    const onMount = () => {
      mounts += 1;
    };
    const view = render(
      <ErrorBoundary resetKey={1}>
        <MountProbe onMount={onMount} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("probe content")).toBeTruthy();
    expect(mounts).toBe(1);

    view.rerender(
      <ErrorBoundary resetKey={2}>
        <MountProbe onMount={onMount} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("probe content")).toBeTruthy();
    expect(mounts).toBe(1);
  });

  it("changing resetKey clears an error so the incoming subtree renders", async () => {
    let shouldThrow = true;
    function MaybeThrow() {
      if (shouldThrow) {
        throw new Error("boom");
      }
      return <p>recovered</p>;
    }
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {
      /* silence the boundary's crash log in this test */
    });

    const view = render(
      <ErrorBoundary resetKey={1}>
        <MaybeThrow />
      </ErrorBoundary>,
    );
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    expect(screen.getByText("boom")).toBeTruthy();

    shouldThrow = false;
    view.rerender(
      <ErrorBoundary resetKey={2}>
        <MaybeThrow />
      </ErrorBoundary>,
    );
    expect(await screen.findByText("recovered")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    consoleSpy.mockRestore();
  });

  it("keeps the fallback without a resetKey change; Retry recovers once the child stops throwing", async () => {
    let shouldThrow = true;
    function MaybeThrow() {
      if (shouldThrow) {
        throw new Error("boom");
      }
      return <p>recovered</p>;
    }
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {
      /* silence the boundary's crash log in this test */
    });

    render(
      <ErrorBoundary>
        <MaybeThrow />
      </ErrorBoundary>,
    );
    expect(await screen.findByText("Something went wrong")).toBeTruthy();

    // Same props, same failure: the boundary stays on its fallback.
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Something went wrong")).toBeTruthy();

    // The child recovers; Retry now gets past it.
    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("recovered")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    consoleSpy.mockRestore();
  });
});
