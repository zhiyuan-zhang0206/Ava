// API base resolution runs once per module graph (once on the server and once
// in the browser). Keeping it outside api.ts lets telemetry use the same base
// without creating an api.ts ↔ telemetry.ts import cycle.
export const API_BASE = ((): string => {
  if (process.env.NEXT_PUBLIC_API_BASE) return process.env.NEXT_PUBLIC_API_BASE;
  if (typeof window === "undefined") return "";
  // Frontend (:3000) and gateway (:8000 by default) are co-located, so the
  // browser keeps the current hostname and switches only to the gateway port.
  const port = process.env.NEXT_PUBLIC_GATEWAY_PORT ?? "8000";
  const { hostname, protocol } = window.location;
  return `${protocol}//${hostname}:${port}`;
})();
