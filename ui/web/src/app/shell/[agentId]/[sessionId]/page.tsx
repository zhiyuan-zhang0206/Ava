"use client";

// /shell/{agentId}/{sessionId} — full-screen live tail of one of an agent's
// persistent shell sessions. Opened from the inspector panel's shell list;
// polls GET /api/agents/{id}/shell/{sid}?lines=N every 3s and renders the
// terminal output in a monospace pane. The pane sticks to the bottom
// (newest output) unless the user has scrolled up to read history.
//
// Terminal theme: defaults to the system `prefers-color-scheme`; a header
// button toggles system → light → dark → system. The choice is a DB-backed user
// setting (display.shell_terminal_theme) so it survives refreshes and syncs
// across frontends.

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Monitor, Moon, RefreshCw, Sun } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useNow } from "@/lib/use-now";
import { useUserSettings } from "@/lib/use-user-settings";
import { formatShort, formatUptime } from "@/lib/time";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

const POLL_MS = 3000;
const DEFAULT_LINES = 200;
const MIN_LINES = 50;
const MAX_LINES = 2000;

type TerminalTheme = "system" | "light" | "dark";

/** Persisted terminal theme preference + cycle toggle. DB-backed so the choice
 *  follows the user across frontends. */
function useTerminalTheme(): [TerminalTheme, () => void] {
  const { settings, setSetting } = useUserSettings();
  const raw = settings["display.shell_terminal_theme"];
  const theme: TerminalTheme = raw === "light" || raw === "dark" || raw === "system" ? raw : "system";

  const toggle = useCallback(() => {
    const next: TerminalTheme = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
    setSetting("display.shell_terminal_theme", next);
  }, [theme, setSetting]);

  return [theme, toggle];
}

/** Resolve "system" against `prefers-color-scheme: dark` in real time. */
function useResolvedTheme(theme: TerminalTheme): "light" | "dark" {
  const [systemDark, setSystemDark] = useState(() => {
    if (typeof window === "undefined") return true;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  if (theme === "light") return "light";
  if (theme === "dark") return "dark";
  return systemDark ? "dark" : "light";
}

export default function ShellMonitorPage({
  params,
}: {
  params: Promise<{ agentId: string; sessionId: string }>;
}) {
  // Next.js 16 passes params as a Promise.  Unwrap it with useState+useEffect
  // (not use(), to avoid Suspense — the page stays self-contained).
  const [resolved, setResolved] = useState<{ agentId: string; sessionId: string } | null>(null);
  useEffect(() => {
    let cancelled = false;
    params
      .then((p) => { if (!cancelled) setResolved(p); })
      .catch(() => undefined); // Promise rejection is harmless — page stays on "invalid params"
    return () => { cancelled = true; };
  }, [params]);

  const agentId = resolved ? Number(resolved.agentId) : Number.NaN;
  const sessionId = resolved ? Number(resolved.sessionId) : Number.NaN;
  const validParams = resolved !== null && Number.isFinite(agentId) && Number.isFinite(sessionId);

  const [lines, setLines] = useState(DEFAULT_LINES);
  const [inputValue, setInputValue] = useState(String(DEFAULT_LINES));
  const [theme, toggleTheme] = useTerminalTheme();
  const resolvedTheme = useResolvedTheme(theme);
  const isDark = resolvedTheme === "dark";

  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ["agent-shell", agentId, sessionId, lines],
    queryFn: () => api.getAgentShell(agentId, sessionId, lines),
    refetchInterval: POLL_MS,
    retry: false,
    // Fetch on every entry, not just cold. staleTime 0 (not refetchOnMount:
    // "always", which is evaluated at the disabled first mount and defeated by
    // the params-gated `enabled` transition) keeps the capture always-stale, so
    // the moment params resolve and the query enables it pulls fresh — the global
    // 5min staleTime would otherwise show a cached capture until the next 3s poll.
    // Cached output stays on screen while the refetch runs (no blank flash).
    staleTime: 0,
    // Don't fire the query for NaN params (e.g. /shell/NaN/NaN from a
    // broken link) — show the invalid-params message instantly.
    enabled: validParams,
  });

  useEffect(() => {
    document.title = `Agent #${agentId} Shell #${sessionId}`;
  }, [agentId, sessionId]);

  // Commit the input value to `lines` after the user finishes typing (Enter or blur).
  const commitLines = useCallback(() => {
    const n = Number(inputValue);
    if (Number.isFinite(n) && n >= MIN_LINES && n <= MAX_LINES) {
      setLines(n);
    } else {
      // Revert to the current valid value.
      setInputValue(String(lines));
    }
  }, [inputValue, lines]);

  // Stick the pane to the bottom on new output, but only when the user is
  // already near the bottom — so scrolling up to read history isn't yanked
  // away on the next 3s poll.
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  useEffect(() => {
    if (stickRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [data]);

  // Title-bar meta: runtime + TTL from the capture response's timestamps.
  // Runtime is launch → now (ticking via the shared 1s tick, falling back to
  // the probe-time uptime snapshot when created_at is missing); TTL remaining
  // is expires_at − now, "No TTL" when the session records none (watcher /
  // legacy pre-mandate shells).
  const now = useNow(1_000);
  const createdMs = data?.created_at != null ? new Date(data.created_at).getTime() : NaN;
  const runtimeSeconds = Number.isFinite(createdMs)
    ? Math.max(0, Math.floor((now.getTime() - createdMs) / 1000))
    : (data?.uptime_seconds ?? 0);
  const expiresAt = data?.expires_at ?? null;
  const expiresMs = expiresAt != null ? new Date(expiresAt).getTime() : NaN;
  const ttlRemainingSeconds = Number.isFinite(expiresMs)
    ? Math.max(0, Math.floor((expiresMs - now.getTime()) / 1000))
    : null;

  // Terminal colour classes keyed on the resolved theme.
  const terminalBg = isDark ? "bg-black" : "bg-gray-100";
  const terminalFg = isDark ? "text-green-400" : "text-gray-900";
  const terminalMuted = isDark ? "text-muted-foreground" : "text-gray-500";

  return (
    <main id="main-content" className={cn("h-full bg-background text-foreground", FLEX, MIN_H_0, FLEX_COL)}>
      <header className={cn("items-center gap-3 border-b border-border px-3 py-2 sm:px-4", FLEX)}>
        <Link
          href="/"
          aria-label="Back to dashboard"
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        >
          <ArrowLeft className="size-4" />
        </Link>
        <h1 className={cn("truncate font-mono text-sm text-foreground", MIN_W_0, FLEX_1)}>
          Agent <span className="text-muted-foreground">#</span>
          {agentId} Shell <span className="text-muted-foreground">#</span>
          {sessionId}
        </h1>
        {data?.session_name && (
          <span className="hidden shrink-0 truncate font-mono text-[11px] text-muted-foreground sm:inline">
            {data.session_name}
          </span>
        )}

        {/* Runtime + TTL — the session's meta facts, live on the title bar
            (user correction 2026-08-28: moved here from the inspector). */}
        {data && (
          <span className={cn("shrink-0 items-center gap-2 font-mono text-[11px] text-muted-foreground", FLEX)}>
            <span className="tabular-nums">
              Runtime {formatUptime(runtimeSeconds)}
            </span>
            {ttlRemainingSeconds != null && expiresAt != null ? (
              <span className="hidden tabular-nums sm:inline">
                TTL {formatUptime(ttlRemainingSeconds)} · expires {formatShort(expiresAt)}
              </span>
            ) : (
              <span className="hidden sm:inline">No TTL</span>
            )}
          </span>
        )}

        {/* Theme toggle — cycles system → light → dark → system */}
        <button
          type="button"
          onClick={toggleTheme}
          aria-label={`Terminal theme: ${theme}`}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        >
          {theme === "system" ? <Monitor className="size-3.5" /> : theme === "light" ? <Sun className="size-3.5" /> : <Moon className="size-3.5" />}
        </button>

        {/* Lines control */}
        <div className={cn("shrink-0 items-center gap-1.5", FLEX)}>
          <label htmlFor="shell-lines" className="font-sans text-2xs text-muted-foreground">
            Lines
          </label>
          <input
            id="shell-lines"
            type="number"
            min={MIN_LINES}
            max={MAX_LINES}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onBlur={commitLines}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitLines();
            }}
            className="w-14 rounded border border-input bg-background px-1.5 py-0.5 text-center font-mono text-[11px] text-foreground focus:border-ring focus:ring-1 focus:ring-ring focus:outline-none"
          />

          {/* Manual refresh button */}
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching || !validParams}
            aria-label="Refresh shell output"
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
          >
            <RefreshCw className={`size-3.5 ${isFetching ? "animate-spin" : ""}`} />
          </button>
        </div>

        {/* Health indicator — follows the actual poll, not a hardcoded green.
            A failing poll turns it amber + "stale" (the pane keeps showing the
            last-good output below); a healthy poll is green + "live". */}
        <span className={cn("shrink-0 items-center gap-1.5 font-mono text-[11px] text-muted-foreground", FLEX)}>
          <span
            className={`size-1.5 rounded-full ${error ? "bg-amber-500" : "bg-green-500"}`}
          />
          {error ? "Stale" : "Live"}
        </span>
      </header>

      {/* Terminal pane — theme-aware via the toggle above */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={`min-h-0 flex-1 overflow-auto px-3 py-2 sm:px-4 ${terminalBg}`}
      >
        {!validParams ? (
          <p className={`font-mono text-xs ${terminalMuted}`}>
            Invalid agent or session id
          </p>
        ) : error && !data ? (
          // Cold failure only — with no output yet there is nothing to keep
          // showing, so surface the error. Once we have output, a failed poll
          // falls through to the stale pane below (the amber "stale" dot flags it).
          <p className={`font-mono text-xs whitespace-pre-wrap ${isDark ? "text-red-400" : "text-red-700"}`}>
            {error instanceof Error ? error.message : "Failed to load shell output"}
          </p>
        ) : !data ? (
          <p className={`font-mono text-xs ${terminalMuted}`}>Loading…</p>
        ) : data.lines.length === 0 ? (
          <p className={`font-mono text-xs ${terminalMuted}`}>(no output)</p>
        ) : (
          <pre className={`font-mono text-xs leading-relaxed whitespace-pre-wrap break-words sm:text-sm ${terminalFg}`}>
            {data.lines.join("\n")}
          </pre>
        )}
      </div>
    </main>
  );
}
