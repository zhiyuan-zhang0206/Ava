// AuthProvider / useAuth tests.
//
// Pins the auth state machine: mount -> checkAuth resolves the initial status;
// login() flips to authenticated on success and reports "invalid-credentials"
// vs. "network-error" on failure depending on what api.login threw; logout()
// always lands on unauthenticated. api is mocked so no network runs.

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "./api";
import { ApiError, api } from "./api";
import { AuthProvider, useAuth } from "./auth-context";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      checkAuth: vi.fn(),
      login: vi.fn(),
      logout: vi.fn(),
    },
  };
});

const checkAuth = vi.mocked(api.checkAuth);
const login = vi.mocked(api.login);
const logout = vi.mocked(api.logout);

function wrapper({ children }: { children: React.ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}

beforeEach(() => {
  checkAuth.mockReset();
  login.mockReset();
  logout.mockReset();
});

afterEach(cleanup);

describe("AuthProvider mount check", () => {
  it("resolves to authenticated when checkAuth says so", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("authenticated"));
  });

  it("resolves to unauthenticated when checkAuth says so", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));
  });

  it("resolves to unauthenticated when checkAuth rejects", async () => {
    checkAuth.mockRejectedValue(new Error("network"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));
  });
});

describe("login / logout", () => {
  it("login success flips status to authenticated and returns 'ok'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("ok");
    expect(login).toHaveBeenCalledWith("admin", "secret");
    expect(result.current.status).toBe("authenticated");
  });

  it("a real 401 rejection returns 'invalid-credentials' and leaves status unauthenticated", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(401, "HTTP 401: bad password"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("invalid-credentials");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a 403 rejection also returns 'invalid-credentials'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(403, "HTTP 403: forbidden"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("invalid-credentials");
  });

  it("a 429 rejection (login rate-limited) returns 'rate-limited'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(
      new ApiError(429, "HTTP 429: too many failed login attempts"),
    );
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("rate-limited");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a network-level failure (fetch reject, no HTTP response) returns 'network-error'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new TypeError("Failed to fetch"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("network-error");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a 5xx ApiError also returns 'network-error', not 'invalid-credentials'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(503, "HTTP 503: Service Unavailable"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("network-error");
  });

  it("logout flips status to unauthenticated", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    logout.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    await act(async () => {
      await result.current.logout();
    });
    expect(logout).toHaveBeenCalled();
    expect(result.current.status).toBe("unauthenticated");
  });

  it("logout lands on unauthenticated even when api.logout throws", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    logout.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    // logout()'s finally clears status, but the rejection still propagates.
    await act(async () => {
      await expect(result.current.logout()).rejects.toThrow("boom");
    });
    expect(result.current.status).toBe("unauthenticated");
  });
});

describe("useAuth outside provider", () => {
  it("throws a clear error", () => {
    // Silence the React error-boundary console noise for the expected throw.
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => renderHook(() => useAuth())).toThrow(
      /useAuth must be used inside/,
    );
    spy.mockRestore();
  });
});
