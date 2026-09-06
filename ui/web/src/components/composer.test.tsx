// Composer integration tests — locks button (mode, draft) dispatch +
// Enter / IME guard + input-preserved-on-send-failure. Key invariants:
// busy + empty = stop, busy + non-empty = send (new message goes
// through the inbound queue; agent picks it up after the current turn);
// Enter always sends; stop is only triggered by clicking with empty
// input.
//
// happy-dom + RTL — vitest globals=false; explicit cleanup, mirrors
// connection-notice.test.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, MessageDeliveryUnknownError } from "@/lib/api";
import { clearMessageSent, markMessageSent } from "@/lib/interaction-timing";

import { Composer } from "./composer";

// The Composer mounts SlashAutocomplete, which fetches the command list on
// mount. Default to an empty list so the existing button/Enter tests don't hit
// the network; the command-integration block overrides commandList per-test.
// getContextBreakdown backs the context-breakdown-panel block (mounted only
// when that panel is expanded).
let commandList: { name: string; description: string; instruction_hint: string }[] = [];
vi.mock("@/lib/api", () => ({
  MessageDeliveryUnknownError: class MessageDeliveryUnknownError extends Error {
    constructor(readonly clientMessageId: string) {
      super("delivery unconfirmed");
    }
  },
  api: {
    getCommands: vi.fn((_agentId?: number | null) => Promise.resolve(commandList)),
    getContextBreakdown: () =>
      Promise.resolve({
        total_input_tokens: 1000,
        estimated_total: 250,
        max_input_tokens: 1_000_000,
        soft_compact_tokens: 600_000,
        hard_compact_tokens: 800_000,
        sections: [],
        categories: [{ kind: "system_prompt", tokens: 1000 }],
      }),
  },
}));

vi.mock("@/lib/interaction-timing", () => ({
  clearMessageSent: vi.fn(),
  markMessageSent: vi.fn(),
}));

const getCommandsMock = vi.mocked(api.getCommands);

beforeEach(() => {
  vi.mocked(clearMessageSent).mockClear();
  vi.mocked(markMessageSent).mockClear();
  getCommandsMock.mockImplementation((_agentId?: number | null) =>
    Promise.resolve(commandList),
  );
});

afterEach(() => {
  cleanup();
  commandList = [];
  getCommandsMock.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const baseProps = {
  contextTokens: 0,
  onSend: vi.fn(() => Promise.resolve(true)),
  onStop: vi.fn(),
};

function storageWith(
  persistent: Storage,
  overrides: Partial<Pick<Storage, "getItem" | "setItem" | "removeItem">>,
): Storage {
  return {
    get length() {
      return persistent.length;
    },
    clear: persistent.clear.bind(persistent),
    getItem: overrides.getItem ?? persistent.getItem.bind(persistent),
    key: persistent.key.bind(persistent),
    removeItem: overrides.removeItem ?? persistent.removeItem.bind(persistent),
    setItem: overrides.setItem ?? persistent.setItem.bind(persistent),
  };
}

// Once contextTokens > 0, the meta row mounts ContextButton, which reads the
// context-meter-width display setting via useUserSettings (react-query) —
// needs a QueryClient in the tree even when the breakdown panel itself is
// never opened.
function renderComposer(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

/** Render a Composer with the given agentId and return the textarea element. */
function renderComposerAgent(agentId: number) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <Composer {...baseProps} mode="idle" agentId={agentId} />
    </QueryClientProvider>,
  );
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByTestId returns HTMLElement; narrowing to access .value
  return screen.getByTestId("composer-input") as HTMLTextAreaElement;
}

describe("Composer placeholder", () => {
  it("explains that messaging a terminated agent resurrects it", () => {
    const { rerender } = render(<Composer {...baseProps} mode="idle" />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "send a message",
    );

    rerender(<Composer {...baseProps} mode="idle" agentTerminated />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "send a message to resurrect this agent",
    );
  });
});

describe("Composer button mode dispatch", () => {
  // Layout regression (task #714 + #715): the composer is width-capped and
  // centered inside the timeline column. The cap comes from the
  // display.timeline_width_ratio setting via the maxWidthCss prop (inline
  // maxWidth: min(<ratio>vw, 1280px)); #723-⑧ dropped the #715 48px offset
  // so the composer aligns with the timeline column width. The home page
  // passes the ratio-derived cap explicitly on desktop (audit C2 removed the
  // hidden default so absent = full-width everywhere). jsdom can't measure
  // layout, so assert the class + inline-style contract on the composer root.
  it("root is width-capped, centered, aligned with the timeline width", () => {
    render(<Composer {...baseProps} mode="idle" maxWidthCss="min(40vw, 1280px)" />);
    const root = screen.getByTestId("composer");
    expect(root.className).toContain("mx-auto");
    expect(root.className).toContain("w-full");
    expect(root.className).not.toContain("max-w-[45rem]");
    expect(root.style.maxWidth).toBe("min(40vw, 1280px)");
  });

  it("uses the tighter bottom gap without changing the textarea height", () => {
    render(<Composer {...baseProps} mode="idle" />);
    const root = screen.getByTestId("composer");

    expect(root.className).toContain("pb-3");
    expect(root.className).not.toContain("pb-5");
    expect(screen.getByTestId("composer-input").className).toContain("min-h-9");
  });

  it("root maxWidth follows the maxWidthCss prop (setting-derived)", () => {
    render(<Composer {...baseProps} mode="idle" maxWidthCss="min(60vw, 1280px)" />);
    const root = screen.getByTestId("composer");
    expect(root.style.maxWidth).toBe("min(60vw, 1280px)");
  });

  // Task #805: narrow viewports (mobile) pass undefined — no inline maxWidth
  // is rendered and the composer is full-width (w-full + mx-auto). One falsy
  // convention (audit C2): undefined, never "" — a consumer doing a `!= null`
  // check must not silently cap the mobile composer.
  it("undefined maxWidthCss (mobile) → no inline maxWidth, full-width", () => {
    render(<Composer {...baseProps} mode="idle" maxWidthCss={undefined} />);
    const root = screen.getByTestId("composer");
    expect(root.style.maxWidth).toBe("");
    expect(root.className).toContain("w-full");
    expect(root.className).toContain("mx-auto");
  });

  it("mode='idle' + non-empty input → click triggers onSend, not onStop", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="idle" onSend={onSend} onStop={onStop} />);

    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    // await microtask for async onSend
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("hello", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });

  it("marks send latency at submit time before message delivery starts", () => {
    const onSend = vi.fn(() => new Promise<boolean>(() => undefined));
    render(<Composer {...baseProps} agentId={42} mode="idle" onSend={onSend} />);
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "hello" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(markMessageSent).toHaveBeenCalledWith(42);
    expect(vi.mocked(markMessageSent).mock.invocationCallOrder[0]).toBeLessThan(
      onSend.mock.invocationCallOrder[0],
    );
  });

  it("mode='busy' + non-empty input → click triggers onSend (goes through inbound queue, doesn't stop turn)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);

    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "draft" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("draft", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });

  it("mode='busy' + empty input → click triggers onStop (cancel path)", () => {
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="busy" onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop current turn" }));
    expect(onStop).toHaveBeenCalled();
  });

  it("mode='busy' typing a char → aria-label switches from 'Stop current turn' to 'Send message'", () => {
    render(<Composer {...baseProps} mode="busy" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");

    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "x" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Send message");

    // Clear input → button flips back to stop (must be bidirectional)
    fireEvent.change(ta, { target: { value: "" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });

  it("mode='busy' + whitespace-only → button still stop (empty after trim; guards against .trim() being silently dropped, flipping to send)", () => {
    render(<Composer {...baseProps} mode="busy" />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "   \n  " } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });

  it("mode='disabled' → button disabled, neither onSend nor onStop fires", () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="disabled" onSend={onSend} onStop={onStop} />);

    const btn = screen.getByRole("button");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(btn);
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("mode='idle' + empty input → button disabled (empty send is meaningless)", () => {
    render(<Composer {...baseProps} mode="idle" />);
    const btn = screen.getByRole("button", { name: "Send message" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it("aria-label follows mode ('Send message' ↔ 'Stop current turn')", () => {
    const { rerender } = render(<Composer {...baseProps} mode="idle" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Send message");

    rerender(<Composer {...baseProps} mode="busy" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });
});

describe("Composer Enter / IME / Shift+Enter", () => {
  it("plain Enter → triggers action (onSend when idle)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "msg" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("msg", [], expect.any(String));
  });

  it("Enter in mode='busy' goes to onSend, not onStop (stop is click-only)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "draft" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("draft", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });

  it("mode='busy' + empty input → Enter no-op (nothing to send, also not stop)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("IME composing (isComposing=true) → Enter does not trigger (CJK pinyin must be protected)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    // RTL fireEvent.keyDown forwards nativeEvent props
    fireEvent.keyDown(ta, { key: "Enter", isComposing: true });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("IME Process key fallback → Enter does not trigger (even with isComposing=false)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Process", isComposing: false });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Shift+Enter → does not trigger send (default newline behavior, let textarea handle it)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "line1" } });
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer input lifecycle", () => {
  it("send succeeds (returns true) → input cleared", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(ta.value).toBe(""));
  });

  it("send fails (returns false) → input preserved, no lost user text (H2 fix)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "important draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    // Wait for onSend microtask to complete — on failure no setValue, input must persist
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(ta.value).toBe("important draft");
  });

  it("mode='busy' send fails (returns false) → input preserved (busy goes through the same submit path; failure branch must be covered)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    render(<Composer {...baseProps} mode="busy" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "queued draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(ta.value).toBe("queued draft");
  });

  it("during sending the button is disabled, repeated clicks fire onSend only once (issue 6 dedup guard)", async () => {
    const deferred: { resolve: (v: boolean) => void } = {
      resolve: () => undefined,
    };
    const onSend = vi.fn<(content: string) => Promise<boolean>>(
      () => new Promise<boolean>((resolve) => { deferred.resolve = resolve; }),
    );
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "first" } });

    // First click enters sending state (Promise unresolved, button disabled)
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "Sending" });
      expect((btn as HTMLButtonElement).disabled).toBe(true);
    });

    // Second / third click blocked while sending
    const sendingBtn = screen.getByRole("button", { name: "Sending" });
    fireEvent.click(sendingBtn);
    fireEvent.click(sendingBtn);
    expect(onSend).toHaveBeenCalledTimes(1);

    // Resolve the promise → button returns to send state
    deferred.resolve(true);
    await waitFor(() => expect(ta.value).toBe(""));
  });

  it("retrying an uncertain send reuses its client message id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "uncertain delivery" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));

    const firstId = onSend.mock.calls[0][2];
    const retryId = onSend.mock.calls[1][2];
    expect(firstId).toEqual(expect.any(String));
    expect(firstId.length).toBeGreaterThan(0);
    expect(retryId).toBe(firstId);
  });

  it("shows an explicit unknown state and retries the same logical message", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    render(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "uncertain delivery" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Delivery unconfirmed/);
    expect(clearMessageSent).not.toHaveBeenCalled();
    expect(ta.readOnly).toBe(true);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled,
    ).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Retry same message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(onSend.mock.calls[0][2]);
    await waitFor(() => expect(ta.value).toBe(""));
    expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull();
  });

  it("keeps an unknown attempt locked when a retry returns false", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    render(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "still ambiguous" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Delivery unconfirmed/);
    const clientMessageId = onSend.mock.calls[0][2];

    fireEvent.click(screen.getByRole("button", { name: "Retry same message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(clientMessageId);
    expect(await screen.findByText(/Delivery unconfirmed/)).toBeTruthy();
    expect(ta.readOnly).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Retry same message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(3));
    expect(onSend.mock.calls[2][2]).toBe(clientMessageId);
    await waitFor(() => expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull());
  });

  it("requires explicit abandon before changed text gets a new id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    render(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "old uncertain" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Delivery unconfirmed/);

    fireEvent.click(screen.getByRole("button", { name: "Send another anyway" }));
    expect(clearMessageSent).toHaveBeenCalledWith(7);
    expect(ta.readOnly).toBe(false);
    fireEvent.change(ta, { target: { value: "new explicit message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).not.toBe(onSend.mock.calls[0][2]);
  });

  it("storage failure falls back to memory without changing retry identity", async () => {
    const agentId = 7071;
    const attemptKey = `composer-send-attempt-${agentId}`;
    const persistentStorage = sessionStorage;
    persistentStorage.setItem(
      attemptKey,
      JSON.stringify({
        signature: JSON.stringify([agentId, "stale body", []]),
        clientMessageId: "stale-client-id",
        agentId,
        content: "stale body",
        imageUrls: [],
      }),
    );
    expect(persistentStorage.getItem(attemptKey)).toContain("stale-client-id");
    const setItem = vi.fn((_key: string, _value: string) => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    vi.stubGlobal("sessionStorage", storageWith(persistentStorage, { setItem }));
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    const first = render(
      <Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "send despite storage" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await screen.findByText(/Delivery unconfirmed/);
    const firstId = onSend.mock.calls[0][2];
    expect(firstId).not.toBe("stale-client-id");
    expect(setItem).toHaveBeenCalled();
    expect(sessionStorage.getItem(attemptKey)).toContain("stale-client-id");
    first.unmount();

    render(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    await screen.findByText(/Delivery unconfirmed/);
    fireEvent.click(screen.getByRole("button", { name: "Retry same message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(firstId);
    const restored = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    await waitFor(() => expect(restored.value).toBe(""));
    expect(screen.queryByRole("button", { name: "Sending" })).toBeNull();
  });

  it("remove failure keeps an authoritative tombstone across remount", async () => {
    const agentId = 7072;
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      );
    const first = render(
      <Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "abandon this unknown" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Delivery unconfirmed/);

    const attemptKey = `composer-send-attempt-${agentId}`;
    const persistedAttempt = sessionStorage.getItem(attemptKey);
    expect(persistedAttempt).toContain("uncertain");
    const persistentStorage = sessionStorage;
    const removeItem = vi.fn((_key: string) => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.stubGlobal("sessionStorage", storageWith(persistentStorage, { removeItem }));
    fireEvent.click(screen.getByRole("button", { name: "Send another anyway" }));
    expect(removeItem).toHaveBeenCalledWith(attemptKey);
    expect(sessionStorage.getItem(attemptKey)).toBe(persistedAttempt);
    first.unmount();

    render(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull();
    expect(screen.getByTestId<HTMLTextAreaElement>("composer-input").readOnly).toBe(false);
  });

  it("an A send completing after switch to B does not clear B's draft", async () => {
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    const { rerender } = render(
      <Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "message for A" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));

    sessionStorage.setItem("composer-draft-9", "draft for B");
    rerender(<Composer {...baseProps} mode="idle" agentId={9} onSend={onSend} />);
    await waitFor(() => expect(ta.value).toBe("draft for B"));
    resolveSend(true);

    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(ta.value).toBe("draft for B");
  });

  it("locks same-agent edits and attachments while a send is in flight", async () => {
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/new.png"));
    render(
      <Composer
        {...baseProps}
        mode="idle"
        agentId={7}
        onSend={onSend}
        onAttachImage={onAttachImage}
      />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "original snapshot" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Sending" });
    expect(ta.readOnly).toBe(true);

    fireEvent.change(ta, { target: { value: "later edit" } });
    const image = new File(["png"], "later.png", { type: "image/png" });
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [image], types: ["Files"] },
    });
    expect(onAttachImage).not.toHaveBeenCalled();

    resolveSend(true);
    await waitFor(() => expect(ta.value).toBe(""));
    expect(screen.queryByRole("button", { name: "Sending" })).toBeNull();
  });

  it("editing a failed draft creates a new client message id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockResolvedValue(false);
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "first draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));

    fireEvent.change(ta, { target: { value: "changed draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));

    expect(onSend.mock.calls[1][2]).not.toBe(onSend.mock.calls[0][2]);
  });

  it("whitespace-only input → does not trigger onSend (button also disabled)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "   \n  " } });
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByRole returns HTMLElement; narrowing needed for .disabled
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer token display", () => {
  it("contextTokens=0 shows invisible spacer (no layout jump)", () => {
    render(<Composer {...baseProps} mode="idle" contextTokens={0} />);
    // The spacer text " " is rendered when contextTokens=0
    const span = screen.getByTestId("composer-meta");
    expect(span.textContent).toBe(" ");
  });

  it("contextTokens > 0 shows formatted context token count", () => {
    renderComposer(<Composer {...baseProps} mode="idle" contextTokens={5000} />);
    const span = screen.getByTestId("composer-meta");
    expect(span.textContent).toBe("Context: 5.0k tokens");
  });

  it("maxContextTokens > 0 shows current/max format", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle" contextTokens={26000} maxContextTokens={1000000} />,
    );
    const span = screen.getByTestId("composer-meta");
    expect(span.textContent).toBe("Context: 26.0k/1.00M tokens");
  });

  it("maxContextTokens=0 hides the /max segment", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle" contextTokens={5000} maxContextTokens={0} />,
    );
    const span = screen.getByTestId("composer-meta");
    expect(span.textContent).toBe("Context: 5.0k tokens");
  });
});

describe("Composer context breakdown panel", () => {
  it("expands anchored above the meta row; the composer input stays interactive", async () => {
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        agentId={7}
      />,
    );
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    // Anchored expansion, not a modal: absolutely positioned, growing upward
    // from the meta row (bottom-full) — the space below (the input) is never
    // covered, and no dialog role locks the page.
    expect(panel.className).toContain("absolute");
    expect(panel.className).toContain("bottom-full");
    expect(screen.queryByRole("dialog")).toBeNull();
    // The composer input stays in the DOM, enabled, and editable while open.
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByTestId returns HTMLElement; narrowing to access .value / .disabled
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "still typing" } });
    expect(ta.value).toBe("still typing");
    // Collapse via the panel's close button — panel unmounts, input untouched.
    fireEvent.click(screen.getByTestId("context-breakdown-close"));
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    expect(ta.value).toBe("still typing");
  });

  it("is mutually exclusive with the slash dropdown (single popover state)", async () => {
    commandList = [{ name: "recap", description: "recap the thread", instruction_hint: "" }];
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        agentId={7}
      />,
    );
    // Open the panel, then start typing a slash command: the dropdown opening
    // must close the panel (both are absolute z-50 layers over the same spot).
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-autocomplete");
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    // And the reverse: expanding the panel dismisses the open dropdown.
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    expect(screen.queryByTestId("slash-autocomplete")).toBeNull();
  });
});

describe("Composer file upload (drag + paste)", () => {
  it("does not show the file-upload hint when no file delivery is in flight", () => {
    render(<Composer {...baseProps} mode="idle" filesUploading={false} />);
    expect(screen.queryByTestId("composer-upload-hint")).toBeNull();
  });

  it("shows the file-upload hint without blocking Enter-to-send", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(
      <Composer
        {...baseProps}
        mode="idle"
        filesUploading
        onSend={onSend}
      />,
    );

    expect(screen.getByTestId("composer-upload-hint").textContent).toBe(
      "Uploading file(s) — Enter sends the text now; files are delivered when the upload finishes.",
    );

    const textarea = screen.getByTestId("composer-input");
    fireEvent.change(textarea, { target: { value: "send this now" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("send this now", [], expect.any(String)),
    );
  });

  it("dropping files calls onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    render(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    const f1 = new File(["a"], "a.png", { type: "image/png" });
    const f2 = new File(["bb"], "b.png", { type: "image/png" });
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [f1, f2], types: ["Files"] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([f1, f2]);
  });

  it("dropping with no files does not call onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    render(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [], types: [] },
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });

  it("pasting files calls onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    render(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    const img = new File(["x"], "img.png", { type: "image/png" });
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [img] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([img]);
  });

  it("pasted images use file delivery and Enter sends text while that upload is in flight", async () => {
    let finishUpload: (() => void) | undefined;
    const onUploadFiles = vi.fn(
      () => new Promise<void>((resolve) => { finishUpload = resolve; }),
    );
    const onSend = vi.fn(() => Promise.resolve(true));
    render(
      <Composer
        {...baseProps}
        mode="idle"
        onSend={onSend}
        onUploadFiles={onUploadFiles}
      />,
    );
    const textarea = screen.getByTestId("composer-input");
    const image = new File(["img"], "paste.png", { type: "image/png" });

    fireEvent.paste(textarea, {
      clipboardData: { files: [image], types: ["Files"] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([image]);
    expect(screen.queryByTestId("composer-attachments")).toBeNull();

    fireEvent.change(textarea, { target: { value: "describe the uploaded file" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "describe the uploaded file",
        [],
        expect.any(String),
      ),
    );
    finishUpload?.();
  });

  it("pasting plain text (no files) does not call onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    render(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [] },
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });
});

describe("Composer slash commands", () => {
  const recap = { name: "recap", description: "recap it", instruction_hint: "a focus" };

  const input = () =>
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- RTL getByTestId returns HTMLElement; narrowing to drive .value / selection
    screen.getByTestId("composer-input") as HTMLTextAreaElement;

  /** Move the caret without touching the text, the way an arrow key does. */
  function moveCaret(ta: HTMLTextAreaElement, pos: number) {
    ta.setSelectionRange(pos, pos);
    fireEvent.keyUp(ta, { key: "ArrowLeft" });
  }

  /** Put `marked` in the composer with the caret where "|" is (default: the
   *  end, which is where the browser leaves it after a change). The dropdown
   *  and the hint are caret-scoped, so the caret has to be driven as
   *  deliberately as the text. */
  function typeInto(ta: HTMLTextAreaElement, marked: string) {
    const caret = marked.indexOf("|");
    const value = marked.replace("|", "");
    fireEvent.change(ta, { target: { value } }); // leaves the caret at the end
    if (caret >= 0 && caret !== value.length) moveCaret(ta, caret);
  }

  it("requests the global command list when no agent is selected", async () => {
    render(<Composer {...baseProps} mode="idle" agentId={null} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(1));
    expect(getCommandsMock).toHaveBeenCalledWith();
  });

  it("requests the selected agent's command list", async () => {
    render(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledWith(7));
  });

  it("fetches each agent once and reuses the cached list when switching back", async () => {
    const compact = {
      name: "compact",
      description: "compact context",
      instruction_hint: "a focus",
    };
    getCommandsMock.mockImplementation((selectedAgentId?: number | null) =>
      Promise.resolve(selectedAgentId === 7 ? [recap] : [compact]),
    );
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} />);

    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={8} />);
    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-compact")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={7} />);
    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
    expect(getCommandsMock.mock.calls).toEqual([[7], [8]]);
  });

  it("logs an agent command fetch failure and retries it after switching back", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    getCommandsMock.mockImplementation((selectedAgentId?: number | null) =>
      selectedAgentId === 7
        ? Promise.reject(new Error("catalog unavailable"))
        : Promise.resolve([]),
    );
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[composer] getCommands failed: catalog unavailable",
      ),
    );
    expect(screen.getByTestId("composer-input")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={8} />);
    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(2));
    rerender(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(3));
    expect(getCommandsMock.mock.calls).toEqual([[7], [8], [7]]);
  });

  it("typing '/' opens the dropdown with available commands", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
  });

  it("cannot apply an open slash option while the send snapshot is locked", async () => {
    commandList = [recap];
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/rec");
    const option = await screen.findByTestId("slash-option-recap");

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Sending" });
    fireEvent.mouseDown(option);
    expect(ta.value).toBe("/rec");

    resolveSend(true);
    await waitFor(() => expect(ta.value).toBe(""));
  });

  const abc = [
    { name: "alpha", description: "", instruction_hint: "" },
    { name: "beta", description: "", instruction_hint: "" },
    { name: "gamma", description: "", instruction_hint: "" },
  ];

  it("ArrowDown / ArrowUp move the active option (and wrap around the ends)", async () => {
    commandList = abc;
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    await screen.findByTestId("slash-option-alpha");
    const sel = () =>
      ["alpha", "beta", "gamma"].find(
        (n) => screen.getByTestId(`slash-option-${n}`).getAttribute("aria-selected") === "true",
      );
    expect(sel()).toBe("alpha");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    expect(sel()).toBe("beta");
    fireEvent.keyDown(ta, { key: "ArrowUp" });
    expect(sel()).toBe("alpha");
    // Up from the first wraps to the last.
    fireEvent.keyDown(ta, { key: "ArrowUp" });
    expect(sel()).toBe("gamma");
    // Down from the last wraps back to the first.
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    expect(sel()).toBe("alpha");
  });

  it("Tab picks the active option — fills `/<name> ` and waits, does not send", async () => {
    commandList = abc;
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrowing to read .value
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/" } });
    await screen.findByTestId("slash-option-alpha");
    fireEvent.keyDown(ta, { key: "ArrowDown" }); // highlight beta
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/beta ");
    expect(onSend).not.toHaveBeenCalled();
    // Caret lands at the end so the user types the instruction right after the
    // space (the whole point of the caretToEnd effect).
    expect(ta.selectionStart).toBe(ta.value.length);
    expect(ta.selectionEnd).toBe(ta.value.length);
  });

  it("once the name is committed, the command's instruction_hint shows in the meta row", async () => {
    commandList = [recap]; // instruction_hint: "a focus"
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    const meta = () => screen.getByTestId("composer-meta").textContent;
    // Not in instruction mode yet (no whitespace) — meta row shows the spacer,
    // not the hint (the dropdown renders its own copy, hence the testid scope).
    fireEvent.change(ta, { target: { value: "/recap" } });
    await screen.findByTestId("slash-option-recap");
    expect(meta()).toBe(" ");
    // Whitespace commits the name → hint appears in the meta row (hand-typed too).
    fireEvent.change(ta, { target: { value: "/recap " } });
    await waitFor(() => expect(meta()).toBe("a focus"));
  });

  it("no hint row for an unknown command or a command with an empty hint — falls back to context", async () => {
    commandList = abc; // alpha/beta/gamma all have instruction_hint: ""
    const meta = () => screen.getByTestId("composer-meta").textContent;
    // contextTokens > 0 so the fallback is distinguishable from the spacer.
    renderComposer(<Composer {...baseProps} mode="idle" contextTokens={500} />);
    const ta = screen.getByTestId("composer-input");
    // Known command, but empty instruction_hint → no hint, show the context readout.
    fireEvent.change(ta, { target: { value: "/alpha " } });
    await waitFor(() => expect(meta()).toBe("Context: 500 tokens"));
    // Unknown command → same fallback, no stray hint.
    fireEvent.change(ta, { target: { value: "/zzz " } });
    expect(meta()).toBe("Context: 500 tokens");
  });

  it("Enter on a partial name fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrowing to read .value
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    expect(onSend).not.toHaveBeenCalled();
    // Dropdown closes once whitespace follows the command name — the user
    // is now typing the instruction. The meta row shows the command's hint.
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("after filling, typing the instruction + Enter sends the raw `/<name> <instruction>`", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrowing to read .value
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    // User types the natural-language instruction after the name, then sends.
    fireEvent.change(ta, { target: { value: "/recap just the PRs" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap just the PRs", [], expect.any(String)),
    );
  });

  it("a fully-typed slash message (whitespace present) sends raw — dropdown is open but Enter sends", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/recap just the PRs" } });
    // Dropdown is closed because whitespace follows the command name —
    // the user is typing the instruction, not the command name.
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    // Enter sends the raw text, not re-selects a command.
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap just the PRs", [], expect.any(String)),
    );
  });

  it("clicking a command option fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrowing to read .value
    const ta = screen.getByTestId("composer-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/" } });
    const option = await screen.findByTestId("slash-option-recap");
    fireEvent.mouseDown(option);
    await waitFor(() => expect(ta.value).toBe("/recap "));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Escape dismisses the dropdown, then Enter sends the raw text", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/recap" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("slash-option-recap")).toBeNull());
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap", [], expect.any(String)),
    );
  });

  it("a non-matching /token sends as plain text (no dropdown)", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/nope" } });
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/nope", [], expect.any(String)),
    );
  });

  // ── Multi-command messages (#836) ──
  // Only the message's FIRST token can be a command: the autocomplete never
  // triggers on a later slash, so a multi-command text passes through to
  // onSend unchanged as plain text.

  const compact = { name: "compact", description: "compact context", instruction_hint: "an instruction hint" };

  it("sends raw multi-command text through to onSend", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/compact /update");
    // No dropdown: the caret's token is "/update", which matches no command.
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-compact")).toBeNull();
    // Enter sends the raw text as-is
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/compact /update", [], expect.any(String)),
    );
  });

  it("a slash mid-message never opens the dropdown — Enter sends the raw text (user ruling #836)", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "hello /compact");
    // The trailing slash is NOT the first token — no dropdown, even though
    // "/compact" is a known command.
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-compact")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("hello /compact", [], expect.any(String)),
    );
  });

  it("the instruction hint follows the FIRST command only (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    const meta = () => screen.getByTestId("composer-meta").textContent;
    // Caret in /compact's instruction → compact's hint.
    typeInto(ta, "/compact ping /recap the PRs");
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
    // Caret further in the text (past a later slash) → still compact's hint:
    // the hint only ever follows the leading command now (#836).
    moveCaret(ta, "/compact ping /recap the".length);
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
    // Caret inside the leading name: the dropdown is up and renders the hint
    // itself, so the meta row stays out of the way.
    moveCaret(ta, "/compact".length);
    await waitFor(() => expect(meta()).toBe(" "));
  });

  it("mid-message slash does not surface a hint (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    const meta = () => screen.getByTestId("composer-meta").textContent;
    typeInto(input(), "heads up /compact ping");
    await waitFor(() => expect(meta()).toBe(" "));
  });

  // ── Caret-scoped dropdown (#1172) ──
  // Every token is served alike: a later command on the line, a command typed
  // into the middle of a message, one wedged in front of existing text. The old
  // state machine anchored on the start of the value and refused all three.

  it("a later command in the message gets no dropdown (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), "/compact ping /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("a command started mid-message gets no dropdown (#836)", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), "look at this /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("a command typed in front of existing text gets a dropdown", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    // Caret sent home, a space typed, then the name: the value neither starts
    // with "/" nor is whitespace-free — the two conditions that used to kill it.
    typeInto(input(), " /rec| the PRs are piling up");
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
  });

  it("a mid-message slash cannot be selected — Enter sends the raw text (#836)", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/compact ping /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/compact ping /rec", [], expect.any(String)),
    );
  });

  it("selecting mid-message keeps both sides and reuses the existing space", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    typeInto(ta, " /rec| the PRs");
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe(" /recap the PRs");
    // Caret past that single (not doubled) space — where the instruction goes.
    expect(ta.selectionStart).toBe(" /recap ".length);
  });

  it("selecting a name that is already spelled out just moves the caret past it", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    // Trailing space already typed, caret sent back into the name.
    typeInto(ta, "/recap| ");
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    // The text can't change, so only the caret commit closes the dropdown —
    // and it has to reach the DOM, not just the component's state.
    expect(ta.value).toBe("/recap ");
    expect(ta.selectionStart).toBe("/recap ".length);
    await waitFor(() => expect(screen.queryByTestId("slash-option-recap")).toBeNull());
  });

  it("a path-shaped token doesn't open the dropdown", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "read /etc/hosts");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("read /etc/hosts", [], expect.any(String)),
    );
  });
});

describe("Composer image attachments", () => {
  // happy-dom lacks object-url plumbing; stub it for the preview lifecycle
  // (defineProperty avoids the unbound-method lint of a bare method read).
  beforeEach(() => {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:preview"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });
  });

  const imageFile = () => new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });

  it("pasting an image attaches a thumbnail and uploads it", async () => {
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    render(<Composer {...baseProps} mode="idle" onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("composer-attachments")).toBeTruthy();
  });

  it("sends text + the resolved image url as onSend's second arg", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    fireEvent.change(ta, { target: { value: "what is this" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "what is this",
        ["/api/agents/7/uploads/shot.png"],
        expect.any(String),
      ),
    );
  });

  it("locks an unknown image snapshot against drop and removal before retry", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    render(
      <Composer
        {...baseProps}
        mode="idle"
        agentId={7}
        onSend={onSend}
        onAttachImage={onAttachImage}
      />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Delivery unconfirmed/);

    const remove = screen.getByRole<HTMLButtonElement>("button", { name: "Remove shot.png" });
    expect(remove.disabled).toBe(true);
    fireEvent.click(remove);
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: {
        files: [new File(["new"], "new.png", { type: "image/png" })],
        types: ["Files"],
      },
    });
    expect(onAttachImage).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "shot.png" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Retry same message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][1]).toEqual(["/api/agents/7/uploads/shot.png"]);
    expect(onSend.mock.calls[1][2]).toBe(onSend.mock.calls[0][2]);
  });

  it("send stays disabled while an image is still uploading", async () => {
    let resolve!: (u: string) => void;
    const onAttachImage = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
    render(<Composer {...baseProps} mode="idle" onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "hi" } });
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrow to read .disabled
    const btn = screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    resolve("/api/agents/7/uploads/shot.png");
    await waitFor(() => expect(btn.disabled).toBe(false));
  });

  it("image-only (no text) is sendable once uploaded", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/a.png"));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "",
        ["/api/agents/7/uploads/a.png"],
        expect.any(String),
      ),
    );
  });
});

// ── sessionStorage draft persistence ──
// When switching between agents via the sidebar, the Composer should preserve
// the draft text per agent so the user doesn't lose what they typed.

describe("Composer sessionStorage draft persistence", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  const draftKey = (id: number) => `composer-draft-${id}`;

  it("restores the draft from sessionStorage on mount", () => {
    sessionStorage.setItem(draftKey(7), "hello agent 7");
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    const ta = renderComposerAgent(7) as HTMLTextAreaElement;
    expect(ta.value).toBe("hello agent 7");
  });

  it("starts empty when no draft exists in sessionStorage", () => {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    const ta = renderComposerAgent(7) as HTMLTextAreaElement;
    expect(ta.value).toBe("");
  });

  it("saves the draft to sessionStorage as the user types", () => {
    renderComposerAgent(7);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "draft text" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("draft text");
  });

  it("removes the draft from sessionStorage when the input is cleared", () => {
    renderComposerAgent(7);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "something" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("something");
    fireEvent.change(ta, { target: { value: "" } });
    expect(sessionStorage.getItem(draftKey(7))).toBeNull();
  });

  it("clears the draft on a successful send", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "send this" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("send this");
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(sessionStorage.getItem(draftKey(7))).toBeNull();
  });

  it("does NOT clear the draft on a failed send", async () => {
    const onSend = vi.fn(() => Promise.resolve(false));
    render(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "failed send" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("failed send");
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    await waitFor(() => expect(clearMessageSent).toHaveBeenCalledWith(7));
    // Draft survives a failed send.
    expect(sessionStorage.getItem(draftKey(7))).toBe("failed send");
  });

  it("switches draft when agentId prop changes", () => {
    // Start with agent 7, type some text.
    const { rerender } = render(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "for agent 7" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");

    // Pre-seed a draft for agent 9.
    sessionStorage.setItem(draftKey(9), "for agent 9");

    // Switch to agent 9.
    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    // The old agent's draft stays saved.
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe(
      "for agent 9",
    );
  });

  it("does not write to sessionStorage when agentId is null", () => {
    render(<Composer {...baseProps} mode="disabled" agentId={null} />);
    const ta = screen.getByTestId("composer-input");
    // Composer is disabled with no agent, but the textarea still receives
    // change events in tests — verify no draft is written.
    fireEvent.change(ta, { target: { value: "no agent" } });
    // There should be no draft key for any agent.
    for (let i = 0; i < 100; i++) {
      expect(sessionStorage.getItem(draftKey(i))).toBeNull();
    }
  });

  it("clears old draft on empty input after switching agents", () => {
    const { rerender } = render(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "text for 7" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("text for 7");

    // Switch to agent 9 with no pre-existing draft.
    sessionStorage.removeItem(draftKey(9));
    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe("");

    // The old draft for agent 7 is still there.
    expect(sessionStorage.getItem(draftKey(7))).toBe("text for 7");
  });
});

describe("Composer top-right details slot", () => {
  it("renders the details slot at the right edge of the meta row", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle" details={<button type="button">DETAILS</button>} />,
    );
    expect(screen.getByRole("button", { name: "DETAILS" })).toBeTruthy();
    // The selector sits in the meta row, right-aligned (ml-auto cluster).
    const metaRow = screen.getByTestId("composer-meta").closest("div");
    const cluster = screen.getByRole("button", { name: "DETAILS" }).parentElement!;
    expect(cluster.className).toContain("ml-auto");
    expect(metaRow?.contains(cluster)).toBe(true);
  });

  it("renders no right-edge cluster when neither slot is given", () => {
    renderComposer(<Composer {...baseProps} mode="idle" />);
    const metaRow = screen.getByTestId("composer-meta").closest("div");
    expect(metaRow?.querySelector(".ml-auto")).toBeNull();
  });
});
