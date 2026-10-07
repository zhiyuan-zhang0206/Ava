// useClientReady — "is this frame shaped by the client, not the server
// snapshot?" The home layout's placeholder gate keys on it so a hydrating
// load keeps the SSR-safe frame (placeholder) while a client-side navigation
// renders its real frame in the first commit.

import { render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { useClientReady } from "./use-client-ready";

function Probe() {
  return <span data-testid="ready">{String(useClientReady())}</span>;
}

describe("useClientReady", () => {
  it("is false through SSR (the server snapshot)", () => {
    expect(renderToString(<Probe />)).toContain(">false<");
  });

  it("is true on a client mount, in the first render", () => {
    const { getByTestId } = render(<Probe />);
    expect(getByTestId("ready").textContent).toBe("true");
  });
});
