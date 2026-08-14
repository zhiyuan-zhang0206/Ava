// LoginForm tests — submit wiring + error surfacing.
//
// useAuth().login is mocked. Verifies: button disabled until a password is
// typed; submit forwards (username defaulting to "admin", trimmed);
// "invalid-credentials" shows "Invalid password"; "network-error" (backend
// down / unreachable — NOT the same as a real 401) shows a connection error,
// never "Invalid password".

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "@/lib/auth-context";

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

import { LoginForm } from "./login-form";

const mockAuth = vi.mocked(useAuth);
const loginFn = vi.fn();

beforeEach(() => {
  loginFn.mockReset();
  mockAuth.mockReturnValue({
    status: "unauthenticated",
    login: loginFn,
    logout: vi.fn(),
  });
});

afterEach(cleanup);

function typePassword(value: string) {
  fireEvent.change(screen.getByLabelText("Password"), { target: { value } });
}

describe("LoginForm", () => {
  it("disables submit until a password is entered", () => {
    render(<LoginForm />);
    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).toHaveProperty("disabled", true);
    typePassword("secret");
    expect(button).toHaveProperty("disabled", false);
  });

  it("submits with a trimmed password, defaulting username to admin", async () => {
    loginFn.mockResolvedValue("ok");
    render(<LoginForm />);
    typePassword("  secret  ");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginFn).toHaveBeenCalledWith("admin", "secret"));
  });

  it("submits with the typed username when provided", async () => {
    loginFn.mockResolvedValue("ok");
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "  alice  " },
    });
    typePassword("pw");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginFn).toHaveBeenCalledWith("alice", "pw"));
  });

  // Real 401/403 — the gateway is up and rejected the credentials.
  it("shows 'Invalid password' when login resolves 'invalid-credentials'", async () => {
    loginFn.mockResolvedValue("invalid-credentials");
    render(<LoginForm />);
    typePassword("wrong");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Invalid password",
    );
  });

  // Fetch reject / timeout / 5xx — the backend never actually evaluated the
  // password, so this must NOT read as "Invalid password" (the bug this
  // guards: a down backend used to masquerade as a wrong password).
  it("shows a connection error, not 'Invalid password', when login resolves 'network-error'", async () => {
    loginFn.mockResolvedValue("network-error");
    render(<LoginForm />);
    typePassword("secret");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Connection failed/);
    expect(alert.textContent).not.toMatch(/Invalid password/);
  });

  // 429 — the gateway's login rate limiter locked this IP; the credential
  // was never evaluated, so this must read as "try again later", never as
  // "Invalid password" or a connection failure.
  it("shows a rate-limit message, not 'Invalid password', when login resolves 'rate-limited'", async () => {
    loginFn.mockResolvedValue("rate-limited");
    render(<LoginForm />);
    typePassword("secret");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Too many failed attempts/);
    expect(alert.textContent).not.toMatch(/Invalid password/);
    expect(alert.textContent).not.toMatch(/Connection failed/);
  });

  it("does not call login when the password is only whitespace", () => {
    render(<LoginForm />);
    typePassword("   ");
    // Button is disabled; submitting the form directly hits the early return.
    fireEvent.submit(screen.getByLabelText("Password").closest("form")!);
    expect(loginFn).not.toHaveBeenCalled();
  });
});
