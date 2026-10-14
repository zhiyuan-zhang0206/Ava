// OpenNoticeDetail — the shared open-notice reply surface used by the inspector
// (and, after the queue-merge cut, the fleet queue). Verifies the resolve calls
// per notice kind, the empty-reply guard, the 409 message, and onResolved.
//
// api.resolveNotice is the only network dependency; it is fully mocked, so no
// gateway is touched. The component uses no react-query, so it renders bare.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpenNoticeDetail } from "./open-notice-detail";
import { formatAbsolute, formatRelative } from "@/lib/format/time";
import type { OpenNotice } from "@/lib/contracts/types";

// vi.hoisted so the fn exists before the hoisted vi.mock factory runs.
const { resolveNotice } = vi.hoisted(() => ({
  resolveNotice:
    vi.fn<(agentId: number, noticeId: number, body: unknown, key?: string) => Promise<{ status: string }>>(),
}));
vi.mock("@/lib/transport/api", () => ({ api: { resolveNotice } }));

function ntc(overrides: Partial<OpenNotice> = {}): OpenNotice {
  return {
    id: 11,
    title: "Approve deploy?",
    content: "push to prod?",
    priority: "P0",
    require_response: true,
    blocking: false,
    created_at: "2026-06-14T12:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  resolveNotice.mockReset();
});

describe("OpenNoticeDetail — timestamp", () => {
  const createdAt = "2026-06-14T12:00:00Z";
  const timestamp = `${formatRelative(createdAt)}, ${formatAbsolute(createdAt)}`;

  it("hides the created time by default", () => {
    render(<OpenNoticeDetail agentId={7} notice={ntc({ created_at: createdAt })} />);

    expect(screen.queryByText(timestamp)).toBeNull();
  });

  it("shows the created time when requested", () => {
    render(
      <OpenNoticeDetail
        agentId={7}
        notice={ntc({ created_at: createdAt })}
        showTimestamp
      />,
    );

    expect(screen.getByText(timestamp)).toBeTruthy();
  });

  it("shows expiry time when expire_at is present", () => {
    const expireAt = "2026-06-15T12:00:00Z";
    render(<OpenNoticeDetail agentId={7} notice={ntc({ expire_at: expireAt })} />);
    expect(screen.getByText(new RegExp(formatRelative(expireAt)))).toBeTruthy();
  });
});

describe("OpenNoticeDetail — require_response", () => {
  it("uses the same monospace input typography as the page composer", () => {
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    expect(screen.getByRole<HTMLTextAreaElement>("textbox").className).toContain("font-mono");
  });

  it("answer: sends action=answer with the typed reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "ship it" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", reply: "ship it" }, expect.any(String)),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("send stays disabled until a non-empty reply is typed", () => {
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    const send = screen.getByLabelText("Send answer");
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "x" } });
    expect(send.hasAttribute("disabled")).toBe(false);
  });

  it("dismiss: sends action=dismiss with no reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.click(screen.getByText("Dismiss"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "dismiss" }, expect.any(String)),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("surfaces a friendly message on a 409 (already resolved)", async () => {
    resolveNotice.mockRejectedValue(new Error("HTTP 409 conflict"));
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "x" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(screen.getByText("This notice was already answered.")).toBeTruthy(),
    );
  });
});

describe("OpenNoticeDetail — FYI", () => {
  it("reuses the original read-reply key on a manual retry after response loss", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce({ status: "idling" });
    const onResolved = vi.fn();
    render(<OpenNoticeDetail agentId={7} notice={ntc({ require_response: false })} onResolved={onResolved} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "noted" } });
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(screen.getByText(/response lost/)).toBeTruthy());
    expect(resolveNotice).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(resolveNotice.mock.calls[1]).toEqual(resolveNotice.mock.calls[0]);
    expect(resolveNotice.mock.calls[0][3]).toEqual(expect.any(String));
  });

  it("allocates a new key when the reply changes after a failed submission", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce({ status: "idling" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ require_response: false })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "first" } });
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(screen.getByText(/response lost/)).toBeTruthy());
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "second" } });
    fireEvent.click(screen.getByText("Mark read"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledTimes(2));
    expect(resolveNotice.mock.calls[1][3]).not.toBe(resolveNotice.mock.calls[0][3]);
    expect(resolveNotice.mock.calls[1][2]).toEqual({ action: "read", reply: "second" });
  });

  it("sends one request while repeated Enter events arrive during a pending reply", () => {
    resolveNotice.mockImplementation(() => new Promise(() => {
      // Keep the request unresolved while repeated keyboard events arrive.
    }));
    render(<OpenNoticeDetail agentId={7} notice={ntc({ require_response: false })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "noted" } });
    fireEvent.keyDown(screen.getByRole<HTMLTextAreaElement>("textbox"), { key: "Enter" });
    fireEvent.keyDown(screen.getByRole<HTMLTextAreaElement>("textbox"), { key: "Enter" });
    expect(resolveNotice).toHaveBeenCalledTimes(1);
  });

  it("mark read: sends action=read with no reply when empty, and shows no Send button", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(
      <OpenNoticeDetail
        agentId={7}
        notice={ntc({ id: 22, require_response: false })}
        onResolved={onResolved}
      />,
    );

    expect(screen.queryByLabelText("Send answer")).toBeNull();
    expect(screen.getByText("FYI")).toBeTruthy();
    fireEvent.click(screen.getByText("Mark read"));

    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read" }, expect.any(String)));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("mark read on an already-read notice is silent — no error, queue advances", async () => {
    // User ruling 2026-08-28: the gateway returns 201 for a read on an
    // already-resolved notice (idempotent close), so the UI must advance
    // without surfacing the stale "This notice was already read." error.
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(
      <OpenNoticeDetail
        agentId={7}
        notice={ntc({ id: 22, require_response: false })}
        onResolved={onResolved}
      />,
    );

    fireEvent.click(screen.getByText("Mark read"));

    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read" }, expect.any(String)));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    expect(screen.queryByText("This notice was already read.")).toBeNull();
    expect(screen.queryByText(/failed/i)).toBeNull();
  });

  it("mark read carries an optional note as reply", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ id: 22, require_response: false })} />);

    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "noted" } });
    fireEvent.click(screen.getByText("Mark read"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read", reply: "noted" }, expect.any(String)),
    );
  });
});

const options = [{ id: "web", label: "Web" }, { id: "desktop", label: "Desktop" }];

describe("OpenNoticeDetail — numbered questions", () => {
  it("does not preselect or submit a single choice until explicit confirmation", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "single_select", options, allow_other: true } })} />);
    const submit = screen.getByRole("button", { name: "Submit answer" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" }));
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "2. Desktop" }));
    expect((screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" })).checked).toBe(false);
    expect(resolveNotice).not.toHaveBeenCalled();
    fireEvent.click(submit);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { option_ids: ["desktop"] } }, expect.any(String)));
  });

  it("preserves an Other draft when toggled off, and never selects numbers mentioned in it", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "multi_select", options, allow_other: true } })} />);
    fireEvent.click(screen.getByRole<HTMLInputElement>("checkbox", { name: "2. Desktop" }));
    const other = screen.getByRole<HTMLInputElement>("checkbox", { name: "3. Other" });
    fireEvent.click(other);
    expect(screen.getByRole("button", { name: "Submit answer" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText<HTMLTextAreaElement>("Your alternative answer"), { target: { value: "Based on 2, defer 1." } });
    fireEvent.click(other);
    fireEvent.click(other);
    expect((screen.getByLabelText<HTMLTextAreaElement>("Your alternative answer")).value).toBe("Based on 2, defer 1.");
    expect((screen.getByRole<HTMLInputElement>("checkbox", { name: "1. Web" })).checked).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { option_ids: ["desktop"], other_text: "Based on 2, defer 1." } }, expect.any(String)));
  });

  it("single-select Other replaces the selected option and requires its text", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "single_select", options, allow_other: true } })} />);
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" }));
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "3. Other" }));
    expect(screen.getByRole("button", { name: "Submit answer" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText<HTMLTextAreaElement>("Your alternative answer"), { target: { value: "Use option 2 later." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { option_ids: [], other_text: "Use option 2 later." } }, expect.any(String)));
  });

  it("open questions preserve multiline text; Enter alone does not submit", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "Use 2.\nThen 1." } });
    fireEvent.keyDown(screen.getByRole<HTMLTextAreaElement>("textbox"), { key: "Enter" });
    expect(resolveNotice).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { text: "Use 2.\nThen 1." } }, expect.any(String)));
  });

  it("retains a failed answer and reuses its operation key on a manual retry", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost")).mockResolvedValueOnce({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "Keep it readable." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("response lost"));
    expect((screen.getByRole<HTMLTextAreaElement>("textbox")).value).toBe("Keep it readable.");
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledTimes(2));
    expect(resolveNotice.mock.calls[1]).toEqual(resolveNotice.mock.calls[0]);
  });

  it("disables stale questions after a conflict", async () => {
    resolveNotice.mockRejectedValue(new Error("HTTP 409 expired"));
    render(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "Yes" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("no longer available"));
    expect(screen.getByRole("button", { name: "Submit answer" }).hasAttribute("disabled")).toBe(true);
  });
});
