// OpenNoticeDetail — the shared open-notice reply surface used by the inspector
// (and, after the queue-merge cut, the fleet queue). Verifies the resolve calls
// per notice kind, the empty-reply guard, the 409 message, and onResolved.
//
// api.resolveNotice is the only network dependency; it is fully mocked, so no
// gateway is touched. The component uses no react-query, so it renders bare.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpenNoticeDetail } from "./open-notice-detail";
import { formatAbsolute, formatRelative } from "@/lib/time";
import type { OpenNotice } from "@/lib/types";

// vi.hoisted so the fn exists before the hoisted vi.mock factory runs.
const { resolveNotice } = vi.hoisted(() => ({
  resolveNotice:
    vi.fn<(agentId: number, noticeId: number, body: unknown) => Promise<{ status: string }>>(),
}));
vi.mock("@/lib/api", () => ({ api: { resolveNotice } }));

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
});

describe("OpenNoticeDetail — require_response", () => {
  it("uses the notice UI's sans typography rather than the composer's message-body exception", () => {
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    const textbox = screen.getByRole("textbox");
    expect(textbox.className).toContain("font-sans");
    expect(textbox.className).not.toContain("font-mono");
  });

  it("answer: sends action=answer with the typed reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "ship it" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", reply: "ship it" }),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("send stays disabled until a non-empty reply is typed", () => {
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    const send = screen.getByLabelText("Send answer");
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "x" } });
    expect(send.hasAttribute("disabled")).toBe(false);
  });

  it("dismiss: sends action=dismiss with no reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    render(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.click(screen.getByText("Dismiss"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "dismiss" }),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("surfaces a friendly message on a 409 (already resolved)", async () => {
    resolveNotice.mockRejectedValue(new Error("HTTP 409 conflict"));
    render(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "x" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(screen.getByText("This notice was already answered.")).toBeTruthy(),
    );
  });
});

describe("OpenNoticeDetail — FYI", () => {
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

    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read" }));
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

    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read" }));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    expect(screen.queryByText("This notice was already read.")).toBeNull();
    expect(screen.queryByText(/failed/i)).toBeNull();
  });

  it("mark read carries an optional note as reply", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    render(<OpenNoticeDetail agentId={7} notice={ntc({ id: 22, require_response: false })} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "noted" } });
    fireEvent.click(screen.getByText("Mark read"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 22, { action: "read", reply: "noted" }),
    );
  });
});
