// One-time login link: `<ui>/#ticket=<ticket>`, printed by `ava start`.
//
// The ticket rides in the URL fragment so it never reaches a server log or a
// Referer. It is removed from the address bar *before* it is sent anywhere, so
// it cannot linger in history or be copied from there; the gateway spends it on
// the first redemption and sets the ordinary session cookie.

import { ApiError, api } from "../transport/api";

export type TicketOutcome = "none" | "redeemed" | "rejected" | "unreachable";

const TICKET_PARAM = "ticket";

// React StrictMode runs the mount effect twice in development; the second run
// must join the first redemption (the ticket is single-use) instead of finding
// an already-cleared fragment. Cleared once the redemption settles.
let inFlight: Promise<TicketOutcome> | undefined;

function takeTicketFromLocation(): string | null {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const ticket = params.get(TICKET_PARAM);
  if (!params.has(TICKET_PARAM)) return null;
  params.delete(TICKET_PARAM);
  const rest = params.toString();
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}${rest ? `#${rest}` : ""}`,
  );
  return ticket;
}

async function redeem(ticket: string): Promise<TicketOutcome> {
  try {
    await api.redeemLoginTicket(ticket);
    return "redeemed";
  } catch (e) {
    // The gateway answered and refused (spent, expired, unknown, locked out):
    // a 4xx. Anything else never got a verdict.
    return e instanceof ApiError && e.status < 500 ? "rejected" : "unreachable";
  }
}

export function redeemTicketFromLocation(): Promise<TicketOutcome> {
  if (inFlight !== undefined) return inFlight;
  const ticket = takeTicketFromLocation();
  if (!ticket) return Promise.resolve("none");
  const pending = redeem(ticket);
  inFlight = pending;
  void pending.then(() => {
    if (inFlight === pending) inFlight = undefined;
  });
  return pending;
}
