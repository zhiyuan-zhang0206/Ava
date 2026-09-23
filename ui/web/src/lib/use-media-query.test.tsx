// useMediaQuery — SSR-safe media query reads through useSyncExternalStore.
//
// Two contracts matter here: the server snapshot answers false (the
// mobile-first frame during SSR and hydration), and a CLIENT mount reads the
// real match in its first render — no post-mount flip (that flip painted a
// wrong frame whenever a commit and the passive flush straddled a rendering
// pass).

import { act, cleanup, render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMediaQuery } from "./use-media-query";

type Listener = () => void;

const mediaState = {
  matches: false,
  listeners: new Set<Listener>(),
};

function installMatchMedia(): void {
  mediaState.matches = false;
  mediaState.listeners = new Set();
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: mediaState.matches,
      media: query,
      addEventListener: (_type: "change", listener: Listener) => {
        mediaState.listeners.add(listener);
      },
      removeEventListener: (_type: "change", listener: Listener) => {
        mediaState.listeners.delete(listener);
      },
    })),
  );
}

beforeEach(installMatchMedia);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Probe() {
  return <span data-testid="value">{String(useMediaQuery("(min-width: 768px)"))}</span>;
}

describe("useMediaQuery", () => {
  it("answers the server snapshot (false) during SSR", () => {
    mediaState.matches = true; // the "server" has no viewport; the snapshot wins
    expect(renderToString(<Probe />)).toContain(">false<");
  });

  it("reads the live match on the first client render", () => {
    mediaState.matches = true;
    const { getByTestId } = render(<Probe />);
    expect(getByTestId("value").textContent).toBe("true");
    expect(mediaState.listeners.size).toBe(1); // subscribed
  });

  it("follows change events", () => {
    const { getByTestId } = render(<Probe />);
    expect(getByTestId("value").textContent).toBe("false");

    act(() => {
      mediaState.matches = true;
      for (const listener of mediaState.listeners) listener();
    });
    expect(getByTestId("value").textContent).toBe("true");
  });
});
