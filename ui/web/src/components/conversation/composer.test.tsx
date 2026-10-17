import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, MessageDeliveryUnknownError } from "@/lib/transport/api";
import { clearMessageSent, markMessageSent } from "@/lib/format/interaction-timing";

import { Composer } from "./composer";

let commandList: { name: string; description: string; instruction_hint: string }[] = [];
vi.mock("@/lib/transport/api", () => ({
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
        estimated: false,
        exact_fraction: 1,
        max_input_tokens: 1_000_000,
        soft_compact_tokens: 600_000,
        hard_compact_tokens: 800_000,
        sections: [],
        categories: [{ kind: "system_prompt", tokens: 1000, estimated: false, exact_fraction: 1 }],
      }),
  },
}));

vi.mock("@/lib/format/interaction-timing", () => ({
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

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(ui, { wrapper: ({ children }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
}

const renderComposer = render;

function renderComposerAgent(agentId: number) {
  render(<Composer {...baseProps} mode="idle" agentId={agentId} />);
  return screen.getByTestId<HTMLTextAreaElement>("composer-input");
}

describe("Composer placeholder", () => {
  it("explains that messaging a terminated agent resurrects it", () => {
    const { rerender } = render(<Composer {...baseProps} mode="idle" />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "Send a message",
    );

    rerender(<Composer {...baseProps} mode="idle" agentTerminated />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "Send a message to resurrect this agent",
    );
  });
});

describe("Composer reply suggestion", () => {
  const SUGGESTION = "Yes, go ahead and deploy.";

  function input(): HTMLTextAreaElement {
    return screen.getByTestId<HTMLTextAreaElement>("composer-input");
  }

  it("browses numbered candidates in the placeholder and sends only the accepted text", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={["Use A.", "Use B.", "Use C.", "Use D."]} />);

    expect(input().placeholder).toBe("[1/4] Use A.");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/4] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/4] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[3/4] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    expect(input().value).toBe("");

    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => expect(input().value).toBe("Use D."));
    expect(input().selectionStart).toBe("Use D.".length);
    expect(input().selectionEnd).toBe("Use D.".length);
    fireEvent.keyDown(input(), { key: "Enter" });

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("Use D.", [], expect.any(String)));
    await waitFor(() => expect(input().value).toBe(""));
    expect(input().placeholder).toBe("Send a message");
    expect(fireEvent.keyDown(input(), { key: "ArrowRight" })).toBe(true);
    expect(input().value).toBe("");
  });

  it("stops at the first and last candidate instead of wrapping around", () => {
    render(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B.", "Use C."]} />);

    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");

    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");

    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
  });

  it("restores the selected candidate when an accepted or edited draft is cleared", async () => {
    render(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B.", "Use C."]} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => expect(input().value).toBe("Use B."));

    fireEvent.change(input(), { target: { value: "Use B, with changes." } });
    fireEvent.change(input(), { target: { value: "" } });

    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
  });

  it("keeps the selected candidate after a failed send and shows new candidates after success", async () => {
    const onSend = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const options = ["Use A.", "Use B."];
    const { rerender } = render(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={options} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(input().readOnly).toBe(false));
    expect(input().value).toBe("Use B.");
    fireEvent.change(input(), { target: { value: "" } });
    expect(input().placeholder).toBe("[2/2] Use B.");

    fireEvent.keyDown(input(), { key: "ArrowRight" });
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(input().value).toBe(""));
    expect(input().placeholder).toBe("Send a message");

    rerender(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={["Continue.", "Stop here."]} />);
    expect(input().placeholder).toBe("[1/2] Continue.");
  });

  it("resets to the first candidate for a new response or another agent", () => {
    const options = ["Use A.", "Use B."];
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} suggestions={options} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/2] Use B.");

    rerender(<Composer {...baseProps} mode="idle" agentId={8} suggestions={options} />);
    expect(input().placeholder).toBe("[1/2] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    rerender(<Composer {...baseProps} mode="idle" agentId={8} suggestions={["Explain X.", "Explain Y."]} />);
    expect(input().placeholder).toBe("[1/2] Explain X.");
  });

  it.each(["ArrowUp", "ArrowDown", "ArrowRight"])("leaves %s to the input method during composition", (key) => {
    render(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B."]} />);
    expect(fireEvent.keyDown(input(), { key, isComposing: true })).toBe(true);
    expect(input().placeholder).toBe("[1/2] Use A.");
    expect(input().value).toBe("");
  });

  it.each(["ArrowUp", "ArrowDown"])("leaves modified %s and nonempty drafts alone", (key) => {
    render(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B."]} />);
    for (const modifier of [{ shiftKey: true }, { ctrlKey: true }, { altKey: true }, { metaKey: true }]) {
      expect(fireEvent.keyDown(input(), { key, ...modifier })).toBe(true);
      expect(input().placeholder).toBe("[1/2] Use A.");
    }
    fireEvent.change(input(), { target: { value: "My reply" } });
    expect(fireEvent.keyDown(input(), { key })).toBe(true);
    expect(input().value).toBe("My reply");
    expect(input().placeholder).toBe("Send a message");
  });

  it("shows the suggestion as the placeholder of the empty input", () => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    expect(input().getAttribute("placeholder")).toBe(SUGGESTION);
    expect(input().value).toBe("");
  });

  it("ArrowRight accepts it: the input holds the text and the caret is at the end", async () => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });

    expect(notPrevented).toBe(false);
    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
    await waitFor(() => {
      expect(input().selectionStart).toBe(SUGGESTION.length);
    });
    expect(input().selectionEnd).toBe(SUGGESTION.length);
  });

  it("an accepted suggestion is an ordinary draft: Enter sends it", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={[SUGGESTION]} />);

    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
    fireEvent.keyDown(input(), { key: "Enter" });

    await waitFor(() => {
      expect(onSend).toHaveBeenCalledWith(SUGGESTION, [], expect.any(String));
    });
  });

  it("typing hides it, and emptying the input brings it back", () => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    fireEvent.change(input(), { target: { value: "n" } });
    expect(input().getAttribute("placeholder")).toBe("Send a message");

    fireEvent.change(input(), { target: { value: "" } });
    expect(input().getAttribute("placeholder")).toBe(SUGGESTION);
  });

  it("ArrowRight in a non-empty input moves the caret as usual", () => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    fireEvent.change(input(), { target: { value: "draft" } });

    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });

    expect(notPrevented).toBe(true);
    expect(input().value).toBe("draft");
  });

  it.each([
    ["Shift", { shiftKey: true }],
    ["Ctrl", { ctrlKey: true }],
    ["Alt", { altKey: true }],
    ["Meta", { metaKey: true }],
  ])("%s+ArrowRight does not accept it", (_name, modifier) => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight", ...modifier });

    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });

  it("ArrowRight during IME composition is left to the input method", () => {
    render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight", isComposing: true });

    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });

  it("without a suggestion ArrowRight does nothing special", () => {
    render(<Composer {...baseProps} mode="idle" />);

    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });

    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });

  it("a suggestion that goes away restores the normal placeholder", () => {
    const { rerender } = render(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);

    rerender(<Composer {...baseProps} mode="idle" suggestions={null} />);

    expect(input().getAttribute("placeholder")).toBe("Send a message");
  });

  it("works while the agent is busy: the user may queue a reply", async () => {
    render(<Composer {...baseProps} mode="busy" suggestions={[SUGGESTION]} />);

    fireEvent.keyDown(input(), { key: "ArrowRight" });

    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
  });
});

describe("Composer button mode dispatch", () => {
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

    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "hello" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
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

    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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

    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "x" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Send message");

    fireEvent.change(ta, { target: { value: "" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });

  it("mode='busy' + whitespace-only → button still stop (empty after trim; guards against .trim() being silently dropped, flipping to send)", () => {
    render(<Composer {...baseProps} mode="busy" />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "   \n  " } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });

  it("mode='disabled' → button disabled, neither onSend nor onStop fires", () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    render(<Composer {...baseProps} mode="disabled" onSend={onSend} onStop={onStop} />);

    const btn = screen.getByRole<HTMLButtonElement>("button");
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("mode='idle' + empty input → button disabled (empty send is meaningless)", () => {
    render(<Composer {...baseProps} mode="idle" />);
    const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Send message" });
    expect(btn.disabled).toBe(true);
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("IME composing (isComposing=true) → Enter does not trigger (CJK pinyin must be protected)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Enter", isComposing: true });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("IME Process key fallback → Enter does not trigger (even with isComposing=false)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Process", isComposing: false });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Shift+Enter → does not trigger send (default newline behavior, let textarea handle it)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(ta.value).toBe(""));
  });

  it("send fails (returns false) → input preserved, no lost user text (H2 fix)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "important draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(ta.value).toBe("important draft");
  });

  it("mode='busy' send fails (returns false) → input preserved (busy goes through the same submit path; failure branch must be covered)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    render(<Composer {...baseProps} mode="busy" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "first" } });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => {
      const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Sending" });
      expect(btn.disabled).toBe(true);
    });

    const sendingBtn = screen.getByRole("button", { name: "Sending" });
    fireEvent.click(sendingBtn);
    fireEvent.click(sendingBtn);
    expect(onSend).toHaveBeenCalledTimes(1);

    deferred.resolve(true);
    await waitFor(() => expect(ta.value).toBe(""));
  });

  it("retrying an uncertain send reuses its client message id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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

  it("keeps an unknown draft editable and normal Send reuses its identity", async () => {
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
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(clearMessageSent).not.toHaveBeenCalled();
    expect(ta.readOnly).toBe(false);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled,
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(onSend.mock.calls[0][2]);
    await waitFor(() => expect(ta.value).toBe(""));
    expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull();
  });

  it("keeps the same identity across an ambiguous send and a failed normal retry", async () => {
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
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    const clientMessageId = onSend.mock.calls[0][2];

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(clientMessageId);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(ta.readOnly).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(3));
    expect(onSend.mock.calls[2][2]).toBe(clientMessageId);
    await waitFor(() => expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull());
  });

  it("gives edited text a new identity after an ambiguous send", async () => {
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
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());

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
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    const firstId = onSend.mock.calls[0][2];
    expect(firstId).not.toBe("stale-client-id");
    expect(setItem).toHaveBeenCalled();
    expect(sessionStorage.getItem(attemptKey)).toContain("stale-client-id");
    first.unmount();

    render(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(firstId);
    const restored = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    await waitFor(() => expect(restored.value).toBe(""));
    expect(screen.queryByRole("button", { name: "Sending" })).toBeNull();
  });

  it("successful send keeps an authoritative tombstone when storage removal fails", async () => {
    const agentId = 7072;
    const persistentStorage = sessionStorage;
    const removeItem = vi.fn((_key: string) => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.stubGlobal("sessionStorage", storageWith(persistentStorage, { removeItem }));
    const onSend = vi.fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>(() => Promise.resolve(true));
    const first = render(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "sent message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(ta.value).toBe(""));
    expect(removeItem).toHaveBeenCalledWith(`composer-send-attempt-${agentId}`);
    expect(persistentStorage.getItem(`composer-send-attempt-${agentId}`)).toBeTruthy();
    const firstId = onSend.mock.calls[0][2];
    first.unmount();
    render(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    const restored = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    expect(restored.value).toBe("");
    fireEvent.change(restored, { target: { value: "sent message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).not.toBe(firstId);
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "   \n  " } });
    expect((screen.getByRole<HTMLButtonElement>("button")).disabled).toBe(true);
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer context meter (opt-in)", () => {
  it("renders no context readout by default — those numbers live in the inspector", () => {
    render(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
      />,
    );
    expect(screen.queryByTestId("context-meter")).toBeNull();
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.queryByTestId("context-meter-ghost")).toBeNull();
    expect(screen.getByTestId("composer-meta").textContent).toBe(" ");
  });

  it("keeps the layout spacer when the agent has no LLM call yet", () => {
    render(<Composer {...baseProps} mode="idle" contextTokens={0} showContextMeter />);
    expect(screen.getByTestId("composer-meta").textContent).toBe(" ");
  });

  it("shows the loading ghost while the token snapshot is pending (cold switch)", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle" contextTokens={0} contextPending showContextMeter />,
    );
    expect(screen.getByTestId("context-meter-ghost")).toBeTruthy();
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.getByTestId("composer-meta").textContent).toBe("");
  });

  it("shows the bare gauge — never the numeric readout — when opted in", () => {
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        showContextMeter
      />,
    );
    const span = screen.getByTestId("composer-meta");
    expect(span.querySelector('[data-testid="context-meter"]')).toBeTruthy();
    expect(span.textContent).toBe("");
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
        showContextMeter
        agentId={7}
      />,
    );
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    expect(panel.className).toContain("absolute");
    expect(panel.className).toContain("bottom-full");
    expect(screen.queryByRole("dialog")).toBeNull();
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "still typing" } });
    expect(ta.value).toBe("still typing");
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
        showContextMeter
        agentId={7}
      />,
    );
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-autocomplete");
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
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
    screen.getByTestId<HTMLTextAreaElement>("composer-input");

  function moveCaret(ta: HTMLTextAreaElement, pos: number) {
    ta.setSelectionRange(pos, pos);
    fireEvent.keyUp(ta, { key: "ArrowLeft" });
  }

  function typeInto(ta: HTMLTextAreaElement, marked: string) {
    const caret = marked.indexOf("|");
    const value = marked.replace("|", "");
    fireEvent.change(ta, { target: { value } });
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
    let resolveCompact: (commands: typeof recap[]) => void = () => undefined;
    getCommandsMock.mockImplementation((selectedAgentId?: number | null) =>
      selectedAgentId === 7
        ? Promise.resolve([recap])
        : new Promise((resolve) => { resolveCompact = resolve; }),
    );
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} />);

    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={8} />);
    fireEvent.change(input(), { target: { value: "/" } });
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    resolveCompact([compact]);
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
    fireEvent.keyDown(ta, { key: "ArrowUp" });
    expect(sel()).toBe("gamma");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    expect(sel()).toBe("alpha");
  });

  it("Tab picks the active option — fills `/<name> ` and waits, does not send", async () => {
    commandList = abc;
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    await screen.findByTestId("slash-option-alpha");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/beta ");
    expect(onSend).not.toHaveBeenCalled();
    expect(ta.selectionStart).toBe(ta.value.length);
    expect(ta.selectionEnd).toBe(ta.value.length);
  });

  it("once the name is committed, the command's instruction_hint shows in the meta row", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    const meta = () => screen.getByTestId("composer-meta").textContent;
    fireEvent.change(ta, { target: { value: "/recap" } });
    await screen.findByTestId("slash-option-recap");
    expect(meta()).toBe(" ");
    fireEvent.change(ta, { target: { value: "/recap " } });
    await waitFor(() => expect(meta()).toBe("a focus"));
  });

  it("no hint row for an unknown command or a command with an empty hint — falls back to the readout", async () => {
    commandList = abc;
    const meta = () => screen.getByTestId("composer-meta");
    renderComposer(<Composer {...baseProps} mode="idle" contextTokens={500} showContextMeter />);
    const ta = screen.getByTestId("composer-input");
    const gauge = () => meta().querySelector('[data-testid="context-meter"]');
    fireEvent.change(ta, { target: { value: "/alpha " } });
    await waitFor(() => expect(meta().textContent).toBe(""));
    expect(gauge()).toBeTruthy();
    fireEvent.change(ta, { target: { value: "/zzz " } });
    expect(meta().textContent).toBe("");
    expect(gauge()).toBeTruthy();
  });

  it("Enter on a partial name fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("after filling, typing the instruction + Enter sends the raw `/<name> <instruction>`", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
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
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap just the PRs", [], expect.any(String)),
    );
  });

  it("clicking a command option fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
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

  const compact = { name: "compact", description: "compact context", instruction_hint: "an instruction hint" };

  it("sends raw multi-command text through to onSend", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/compact /update");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-compact")).toBeNull();
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
    typeInto(ta, "/compact ping /recap the PRs");
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
    moveCaret(ta, "/compact ping /recap the".length);
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
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
    expect(ta.selectionStart).toBe(" /recap ".length);
  });

  it("selecting a name that is already spelled out just moves the caret past it", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    typeInto(ta, "/recap| ");
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
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

  it("normal Send preserves an unchanged image snapshot identity after ambiguity", async () => {
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
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());

    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Remove shot.png" }).disabled).toBe(false);
    expect(ta.readOnly).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
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
    const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Send message" });
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

describe("Composer sessionStorage draft persistence", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  const draftKey = (id: number) => `composer-draft-${id}`;

  it("restores the draft from sessionStorage on mount", () => {
    sessionStorage.setItem(draftKey(7), "hello agent 7");
    const ta = renderComposerAgent(7);
    expect(ta.value).toBe("hello agent 7");
  });

  it("starts empty when no draft exists in sessionStorage", () => {
    const ta = renderComposerAgent(7);
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
    expect(sessionStorage.getItem(draftKey(7))).toBe("failed send");
  });

  it("switches draft when agentId prop changes", () => {
    const { rerender } = render(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "for agent 7" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");

    sessionStorage.setItem(draftKey(9), "for agent 9");

    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");
    expect((screen.getByTestId<HTMLTextAreaElement>("composer-input")).value).toBe(
      "for agent 9",
    );
  });

  it("does not write to sessionStorage when agentId is null", () => {
    render(<Composer {...baseProps} mode="disabled" agentId={null} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "no agent" } });
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

    sessionStorage.removeItem(draftKey(9));
    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    expect((screen.getByTestId<HTMLTextAreaElement>("composer-input")).value).toBe("");

    expect(sessionStorage.getItem(draftKey(7))).toBe("text for 7");
  });
});
