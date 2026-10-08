// The cluster view's selection (a root agent and a window) as it travels in the URL.

export interface ClusterSelection {
  root: number;
  /** ISO times. */
  from: string;
  to: string;
}

const DAY_MS = 24 * 3600 * 1000;

function isoOrNull(value: string | null): string | null {
  if (value === null) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : new Date(ms).toISOString();
}

/**
 * Reads `?root=&from=&to=` (what `selectionQuery` writes). `selection` is null unless all three are
 * valid and the window is non-empty; `form` is what to prefill the inputs with — the URL's values
 * where valid, else the last 24 hours up to `now`.
 */
export function parseClusterSelection(
  search: string,
  now: Date,
): { selection: ClusterSelection | null; form: { from: string; to: string } } {
  const params = new URLSearchParams(search);
  const rootText = params.get("root");
  const root = rootText !== null && /^[0-9]+$/.test(rootText) ? Number(rootText) : null;
  const from = isoOrNull(params.get("from")) ?? new Date(now.getTime() - DAY_MS).toISOString();
  const to = isoOrNull(params.get("to")) ?? now.toISOString();
  const fromGiven = isoOrNull(params.get("from")) !== null && isoOrNull(params.get("to")) !== null;
  const ok = root !== null && root >= 1 && fromGiven && Date.parse(from) < Date.parse(to);
  return { selection: ok ? { root, from, to } : null, form: { from, to } };
}

export function selectionQuery(selection: ClusterSelection): string {
  return `?${new URLSearchParams({ root: String(selection.root), from: selection.from, to: selection.to }).toString()}`;
}

/** An ISO time as the `YYYY-MM-DDTHH:mm` a datetime-local input takes, in local time. */
export function toLocalInput(iso: string): string {
  const date = new Date(iso);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
