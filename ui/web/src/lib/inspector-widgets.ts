// Inspector section order — the contract plugin widgets slot into, plus the
// console-side link targets their payloads address.
//
// The built-in sections carry these keys (the panel renders one ordered list:
// built-in sections and plugin widgets merged); a widget's `order` is any int,
// so a value below/above/among these slots it anywhere. Equal orders stack
// built-in sections first, then widgets by (plugin, id) — deterministic, no
// reliance on registration order. Plugin authors read these values from
// `conventions/plugin-spec-v2.md`; renumbering is a deliberate contract change
// (the panel's tests pin the rendered order).

export const INSPECT_SECTION_ORDER = {
  page: 100,
  shells: 200,
  liveness: 300,
  configOverlay: 400,
  cost: 500,
  activity: 600,
  runLink: 700,
  notice: 800,
} as const;

/** The fleet route a resolved taskList row jumps to (the task board's
 *  anchor). The literal return type is load-bearing like `fleetHref`'s: it
 *  lets `next/link` infer the typed-route arm without a cast. */
export function fleetTaskHref(id: number): `/fleet?task=${number}` {
  return `/fleet?task=${id}`;
}

/** The fleet route that opens one notice in the inbox — the notice section's
 *  jump action. Same literal-type contract as `fleetTaskHref`. */
export function fleetNoticeHref(id: number): `/fleet?notice=${number}` {
  return `/fleet?notice=${id}`;
}
