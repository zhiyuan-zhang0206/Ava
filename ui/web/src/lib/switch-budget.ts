// Switch-cost policy for the conversation surface (task #3900 batch 1 / #3894).
//
// One place names the numbers a steady-state switch budget is made of, so the
// inspector policy and its tests read the same definition. Batch 1 owns the
// inspector share; batch 2 (#3900 G2/G4) lands the conversation-side share.
//
// - INSPECTOR_RETENTION_MS: per-agent inspector cache retention — matches the
//   QueryClient global gcTime default. A switch back inside the window renders
//   the cached snapshot immediately (no cold skeleton); past it the visit is
//   cold again and a skeleton is allowed.
// - BACK_VISIT_REVALIDATE_MAX: the most background revalidations one
//   back-visit may fire — the two live inspector reads (current state +
//   statistics; each is independent and fails independently). The widget set
//   is selection-invariant: task events refresh it while open, the 60s
//   interval repairs across gaps, and a reconnect invalidates it — so
//   switching agents alone never refetches it.
//
// Steady-state target the policy serves (batch 1 + batch 2): at most 5
// requests per switch between two warm agents with the panel open, and 0 cold
// skeletons on a back-visit (the 2026-09-18 audit measured 9-10 before
// batch 1).

export const INSPECTOR_RETENTION_MS = 30 * 60_000;
export const BACK_VISIT_REVALIDATE_MAX = 2;
