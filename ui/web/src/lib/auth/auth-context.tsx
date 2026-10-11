"use client";

// Auth context — tracks session authentication state.
//
// On mount, redeems a one-time login ticket from the URL fragment (if any),
// then checks /api/auth/check. While checking, `status === "loading"`.
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

import { ApiError, api } from "../transport/api";
import { redeemTicketFromLocation } from "./login-ticket";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

// A failed login is either the server rejecting the credentials (401/403 —
// the ONLY case that should read as "wrong password" to the user) or the
// request never getting a real answer at all (fetch reject, timeout, CORS,
// or any non-2xx that isn't a credentials rejection — 5xx included). LoginForm
// picks its message off this instead of a bare boolean so a down backend
// can't masquerade as a bad password.
type LoginResult = "ok" | "invalid-credentials" | "rate-limited" | "network-error";

// Why a one-time login link did not sign the user in. Shown on the login form
// only while the user is signed out; the cluster password still works.
type LoginNotice = "ticket-rejected" | "ticket-unreachable";

interface AuthState {
  status: AuthStatus;
  loginNotice: LoginNotice | null;
  login: (username: string, password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);
let sessionInvalidHandler: (() => void) | null = null;

export function setSessionInvalidHandler(handler: (() => void) | null): void {
  sessionInvalidHandler = handler;
}

export function notifySessionInvalid(): void {
  sessionInvalidHandler?.();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [loginNotice, setLoginNotice] = useState<LoginNotice | null>(null);

  useEffect(() => {
    setSessionInvalidHandler(() => setStatus("unauthenticated"));
    return () => setSessionInvalidHandler(null);
  }, []);

  // Check auth on mount. A one-time login link is redeemed first; the session
  // check then decides, so a redeemed cookie that does not actually reach the
  // gateway still lands on the login page.
  useEffect(() => {
    let cancelled = false;
    redeemTicketFromLocation()
      .then((outcome) => {
        if (!cancelled && outcome === "rejected") setLoginNotice("ticket-rejected");
        if (!cancelled && outcome === "unreachable") setLoginNotice("ticket-unreachable");
        return api.checkAuth();
      })
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
        setLoginNotice(null);
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
    <AuthContext.Provider value={{ status, loginNotice, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
