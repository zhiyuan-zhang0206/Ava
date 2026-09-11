// Inspector section order — the contract plugin widgets slot into, plus the
// console-side link targets a jumpButtons widget resolves to.
//
// The built-in sections carry these keys (the panel renders one ordered list:
// built-in sections and plugin widgets merged); a widget's `order` is any int,
// so a value below/above/among these slots it anywhere. Equal orders stack
// built-in sections first, then widgets by (plugin, id) — deterministic, no
// reliance on registration order. Plugin authors read these values from
// `conventions/plugin-spec-v2.md`; renumbering is a deliberate contract change
// (the panel's tests pin the rendered order).

import type { InspectWidgetButton } from "./types";

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

/** The console route a resolved button jumps to, or null when the button's
 *  target did not resolve (or is unknown to this console build — a newer
 *  kernel's target is skipped, never guessed).
 *
 *  The literal return types are load-bearing like `fleetHref`'s: they let
 *  `next/link` infer the typed-route arm without a cast. */
export function jumpButtonHref(
  button: InspectWidgetButton,
): `/fleet?notice=${number}` | `/fleet?task=${number}` | null {
  switch (button.target) {
    case "notice":
      return button.notice_id != null ? `/fleet?notice=${button.notice_id}` : null;
    case "task":
      return button.task_id != null ? `/fleet?task=${button.task_id}` : null;
    default:
      return null;
  }
}
