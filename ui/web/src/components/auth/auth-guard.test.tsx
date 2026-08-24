// AuthGuard tests — the redirect gate.
//
// Pins the auth status x pathname branches:
//   loading            -> spinner, no redirect
//   login page         -> render children (regardless of auth), no redirect
//   authenticated      -> render children
//   unauthenticated off login -> redirect to /login
// useAuth + next/navigation are mocked so the branch is driven directly.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "@/lib/auth-context";

const replaceSpy = vi.fn();
let pathname = "/";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy }),
  usePathname: () => pathname,
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

import { AuthGuard } from "./auth-guard";

const mockAuth = vi.mocked(useAuth);

function setStatus(status: "loading" | "authenticated" | "unauthenticated") {
  mockAuth.mockReturnValue({
    status,
    login: vi.fn(),
    logout: vi.fn(),
  });
}

beforeEach(() => {
  replaceSpy.mockReset();
  pathname = "/";
});

afterEach(cleanup);

describe("AuthGuard", () => {
  it("shows a spinner while loading and does not redirect", () => {
    setStatus("loading");
    const { container } = render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.querySelector(".animate-spin")).toBeTruthy();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("renders children when authenticated", () => {
    setStatus("authenticated");
    render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("redirects to /login when unauthenticated off the login page", () => {
    pathname = "/";
    setStatus("unauthenticated");
    const { container } = render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.firstChild).toBeNull();
    expect(replaceSpy).toHaveBeenCalledWith("/login");
  });

  it("renders children on the login page without redirecting (unauthenticated)", () => {
    pathname = "/login";
    setStatus("unauthenticated");
    render(
      <AuthGuard>
        <span data-testid="child">login</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("renders children on the login page when authenticated", () => {
    pathname = "/login";
    setStatus("authenticated");
    render(
      <AuthGuard>
        <span data-testid="child">login</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
  });
});
