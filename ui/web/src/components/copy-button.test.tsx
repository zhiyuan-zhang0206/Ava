// CopyButton (icon-only, code/command-output blocks) — locks the click
// behavior itself, not just that the button renders. In particular the
// navigator.clipboard-absent path: plain-HTTP LAN deployments have
// navigator.clipboard === undefined even though lib.dom.d.ts types it as
// always-defined, so the execCommand fallback is the actual regression
// surface (see copy-button.tsx / lib/clipboard.ts).

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "./copy-button";

afterEach(cleanup);

describe("CopyButton rendering", () => {
  it("default label — renders with 'Copy code' aria-label", () => {
    render(<CopyButton text="print(1)" />);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeTruthy();
  });

  it("custom label — aria-label reflects it", () => {
    render(<CopyButton text="stdout" label="command output" />);
    expect(screen.getByRole("button", { name: "Copy command output" })).toBeTruthy();
  });

  it("default (streaming=false) — positioned at bottom-right", () => {
    render(<CopyButton text="done" />);
    const btn = screen.getByRole("button", { name: "Copy code" });
    expect(btn.className).toContain("bottom-1.5");
    expect(btn.className).not.toContain("top-1.5");
  });

  it("streaming=true — positioned at top-right so the button stays accessible while tail scrolls", () => {
    render(<CopyButton text="streaming" streaming />);
    const btn = screen.getByRole("button", { name: "Copy code" });
    expect(btn.className).toContain("top-1.5");
    expect(btn.className).not.toContain("bottom-1.5");
  });
});

describe("CopyButton click behavior — navigator.clipboard present", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    });
  });

  it("click → calls navigator.clipboard.writeText with the given text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      writable: true,
      configurable: true,
    });
    render(<CopyButton text="hello world" />);

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await Promise.resolve();

    expect(writeText).toHaveBeenCalledWith("hello world");
  });

  it("click → aria-label flips to 'Copied' as feedback", async () => {
    render(<CopyButton text="hello world" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
  });
});

describe("CopyButton click behavior — navigator.clipboard absent (plain-HTTP LAN)", () => {
  beforeEach(() => {
    // Simulate a non-secure context: navigator.clipboard is undefined.
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      writable: true,
      configurable: true,
    });
  });

  it("click → falls back to document.execCommand('copy') via a temporary textarea", async () => {
    const execCommand = vi.fn().mockReturnValue(true);
    // eslint-disable-next-line @typescript-eslint/no-deprecated, @typescript-eslint/unbound-method -- save the legacy execCommand to restore after the spy; never invoked detached
    const origExec = document.execCommand;
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- spy on legacy execCommand fallback path
    document.execCommand = execCommand as typeof document.execCommand;
    try {
      render(<CopyButton text="fallback text" />);
      fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
      await Promise.resolve();

      expect(execCommand).toHaveBeenCalledWith("copy");
      // Fallback path still gives the same "copied" feedback as the primary path —
      // this is exactly the regression the two parallel CopyButton implementations
      // used to diverge on.
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy(),
      );
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- restore original execCommand
      document.execCommand = origExec;
    }
  });
});
