import type { OpenNotice } from "@/lib/types";

// Merge-adjacent helpers for the Inbox queue. Task #1010 split these out of
// index.tsx so the list modules can share them without an import cycle; the
// R4 W2 data-contract work (useNotices) removes mergeOpen + this file together.

export interface OpenItem {
  agentId: number;
  agentLabel: string;
  notice: OpenNotice;
}

export function fmtLabel(label: string | null, id: number): string {
  return label ? `${label} · #${id}` : `#${id}`;
}
export function openKey(id: number): string {
  return `open:${id}`;
}
export function resolvedKey(id: number): string {
  return `resolved:${id}`;
}
