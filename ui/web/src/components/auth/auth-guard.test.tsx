// AuthGuard tests — the redirect gate.
//
// Pins the five-way branch on auth status x pathname x clusterUpdating:
//   loading            -> spinner, no redirect
//   login page         -> render children (regardless of auth), no redirect
//   authenticated      -> render children
//   unauthenticated + clusterUpdating -> UpdatingPage (not login redirect)
//   unauthenticated + !clusterUpdating -> redirect to /login
// useAuth + next/navigation + store are mocked so the branch is driven directly.

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

// Mock the Zustand store — return clusterUpdating from a module-level variable.
let storeClusterUpdating = false;
vi.mock("@/lib/store", () => ({
  useStore: (selector: (s: { clusterUpdating: boolean }) => unknown) =>
    selector({ clusterUpdating: storeClusterUpdating }),
}));

// Mock UpdatingPage so we can assert it renders.
vi.mock("@/components/updating-page", () => ({
  UpdatingPage: () => <div data-testid="updating-page">System updating...</div>,
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
  storeClusterUpdating = false;
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

  it("shows UpdatingPage for an authenticated session while the cluster is updating", () => {
    setStatus("authenticated");
    storeClusterUpdating = true;
    render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("updating-page")).toBeTruthy();
    expect(screen.queryByTestId("child")).toBeNull();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("returns an authenticated session to the app when the update clears", () => {
    setStatus("authenticated");
    storeClusterUpdating = true;
    const view = render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("updating-page")).toBeTruthy();

    storeClusterUpdating = false;
    view.rerender(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("updating-page")).toBeNull();
    expect(screen.getByTestId("child")).toBeTruthy();
  });

  it("redirects to /login when unauthenticated off the login page (not updating)", () => {
    pathname = "/";
    setStatus("unauthenticated");
    storeClusterUpdating = false;
    const { container } = render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.firstChild).toBeNull();
    expect(replaceSpy).toHaveBeenCalledWith("/login");
  });

  it("shows UpdatingPage when unauthenticated AND cluster is updating", () => {
    pathname = "/";
    setStatus("unauthenticated");
    storeClusterUpdating = true;
    render(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("updating-page")).toBeTruthy();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("renders children on the login page without redirecting (unauthenticated)", () => {
    pathname = "/login";
    setStatus("unauthenticated");
    storeClusterUpdating = false;
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
