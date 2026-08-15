"use client";

// Auth context — tracks session authentication state.
//
// On mount, checks /api/auth/check. While checking, `status === "loading"`.
// Once resolved, `status` is "authenticated" or "unauthenticated".
// Call `login(password)` to authenticate; `logout()` to clear.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api } from "./api";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

// A failed login is either the server rejecting the credentials (401/403 —
// the ONLY case that should read as "wrong password" to the user) or the
// request never getting a real answer at all (fetch reject, timeout, CORS,
// or any non-2xx that isn't a credentials rejection — 5xx included). LoginForm
// picks its message off this instead of a bare boolean so a down backend
// can't masquerade as a bad password.
type LoginResult = "ok" | "invalid-credentials" | "rate-limited" | "network-error";

interface AuthState {
  status: AuthStatus;
  login: (username: string, password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");

  // Check auth on mount
  useEffect(() => {
    let cancelled = false;
    api
      .checkAuth()
      .then((res) => {
        if (!cancelled) setStatus(res.authenticated ? "authenticated" : "unauthenticated");
      })
      .catch(() => {
        if (!cancelled) setStatus("unauthenticated");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(
    async (username: string, password: string): Promise<LoginResult> => {
      try {
        await api.login(username, password);
        setStatus("authenticated");
        return "ok";
      } catch (e) {
        // A well-formed 401/403 means the gateway is up and evaluated the
        // credentials — that's the only case that's actually "wrong
        // password". A 429 means the gateway is up but this IP is locked out
        // of login attempts (rate limiter) — the credential was not even
        // evaluated, so it is a wait-and-retry situation, not a credentials
        // one. Everything else (fetch reject, timeout, 5xx, ...) means the
        // request never got a real answer, which is a reachability problem.
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          return "invalid-credentials";
        }
        if (e instanceof ApiError && e.status === 429) {
          return "rate-limited";
        }
        return "network-error";
      }
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setStatus("unauthenticated");
    }
  }, []);

  return (
    <AuthContext.Provider value={{ status, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
