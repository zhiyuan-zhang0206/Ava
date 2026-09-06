import type { AgentRow } from "@/lib/types";

export interface Props {
  /** Server-driven agent list (TanStack Query cache, kept in sync by useAgents via SSE). */
  agents: AgentRow[];
  /** Per-agent lifecycle pending state (mutation isPending → row spinner). */
  pendingActions: Record<number, "restarting" | "terminating" | "resurrecting" | "compacting">;
  /** Number of SpawningRow placeholders to render (in-flight spawns awaiting AgentSpawned). */
  pendingSpawnCount: number;
  /** True while the initial agent list fetch is in flight (cold load — no cached data yet). */
  isLoading: boolean;
  /** imperative lifecycle actions — provided by the caller's useAgents hook */
  onSpawn: (opts: { machine?: string; model?: string; preset?: string; reasoning_effort?: string }) => void;
  onTerminate: (id: number, force?: boolean) => void;
  onRestart: (id: number) => void;
  onResurrect: (id: number, prompt?: string) => void;
  onFork: (id: number, prompt?: string) => void;
  onCompact: (id: number) => void;
}


// ── Inner props shared by desktop + mobile ──

export interface InnerProps {
  agents: AgentRow[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onSpawn: (opts: { machine?: string; model?: string; preset?: string; reasoning_effort?: string }) => void;
  onTerminate: (id: number, force?: boolean) => void;
  onRestart: (id: number) => void;
  onResurrect: (id: number, prompt?: string) => void;
  onFork: (id: number, prompt?: string) => void;
  onCompact: (id: number) => void;
  pendingActions: Record<number, "restarting" | "terminating" | "resurrecting" | "compacting">;
  pendingSpawnCount: number;
  isLoading: boolean;
  onRename: (id: number, label: string) => void;
  showTerminated: boolean;
  onToggleTerminated: () => void;
  viewMode: "tree" | "flat";
  onToggleViewMode: () => void;
}


export interface DesktopProps extends InnerProps {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  setCollapsed: (c: boolean) => void;
  /** Opens the floating search overlay (search button in the header / rail). */
  onSearchOpen: () => void;
}

export interface MobileProps extends InnerProps {
  mobileOpen: boolean;
  onMobileClose: () => void;
  onSearchOpen: () => void;
}
