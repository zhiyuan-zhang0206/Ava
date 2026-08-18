import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ItemErrorBoundary } from "./item-error-boundary";

afterEach(cleanup);

function Boom({ explode }: { explode: boolean }) {
  if (explode) throw new Error("kaboom");
  return <div>safe content</div>;
}

describe("ItemErrorBoundary", () => {
  it("renders children unchanged when they don't throw", () => {
    render(
      <ItemErrorBoundary resetKey="a">
        <Boom explode={false} />
      </ItemErrorBoundary>,
    );
    expect(screen.getByText("safe content")).toBeTruthy();
  });

  it("degrades a throwing item to an inline fallback that shows the message", () => {
    // React logs caught errors to console.error; silence for a clean run.
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <ItemErrorBoundary resetKey="a">
        <Boom explode={true} />
      </ItemErrorBoundary>,
    );
    expect(screen.getByText(/Failed to render this item: kaboom/)).toBeTruthy();
    spy.mockRestore();
  });

  it("a throwing item does not take down a sibling item (no whole-timeline blank)", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <>
        <ItemErrorBoundary resetKey="a">
          <Boom explode={true} />
        </ItemErrorBoundary>
        <ItemErrorBoundary resetKey="b">
          <Boom explode={false} />
        </ItemErrorBoundary>
      </>,
    );
    expect(screen.getByText(/Failed to render this item: kaboom/)).toBeTruthy();
    expect(screen.getByText("safe content")).toBeTruthy();
    spy.mockRestore();
  });

  it("recovers when resetKey changes and the new content no longer throws", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <ItemErrorBoundary resetKey="a">
        <Boom explode={true} />
      </ItemErrorBoundary>,
    );
    expect(screen.getByText(/Failed to render/)).toBeTruthy();
    rerender(
      <ItemErrorBoundary resetKey="b">
        <Boom explode={false} />
      </ItemErrorBoundary>,
    );
    expect(screen.getByText("safe content")).toBeTruthy();
    spy.mockRestore();
  });
});
