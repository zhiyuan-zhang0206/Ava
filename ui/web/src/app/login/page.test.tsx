// LoginPage tests — session-aware shell around LoginForm.
//
// loading shows the "Checking session" placeholder; an already-authenticated
// session redirects to "/"; otherwise the page renders the login form. useAuth,
// next/navigation, and the LoginForm child are mocked.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "@/lib/auth-context";

const replaceSpy = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/components/auth/login-form", () => ({
  LoginForm: () => <div data-testid="login-form" />,
}));

import LoginPage from "./page";

const mockAuth = vi.mocked(useAuth);

function setStatus(status: "loading" | "authenticated" | "unauthenticated") {
  mockAuth.mockReturnValue({ status, login: vi.fn(), logout: vi.fn() });
}

beforeEach(() => {
  replaceSpy.mockReset();
});

afterEach(cleanup);

describe("LoginPage", () => {
  it("shows a checking-session placeholder while loading", () => {
    setStatus("loading");
    render(<LoginPage />);
    expect(screen.getByText(/Checking session/)).toBeTruthy();
    expect(screen.queryByTestId("login-form")).toBeNull();
  });

  it("renders the login form when unauthenticated", () => {
    setStatus("unauthenticated");
    render(<LoginPage />);
    expect(screen.getByTestId("login-form")).toBeTruthy();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("redirects home when already authenticated", async () => {
    setStatus("authenticated");
    render(<LoginPage />);
    await waitFor(() => expect(replaceSpy).toHaveBeenCalledWith("/"));
  });
});
