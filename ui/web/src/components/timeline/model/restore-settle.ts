import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { SavedScroll } from "@/lib/layout/scroll-memory";

import { materializeBeforeAnchor } from "./materialize-prefix";
import { resolveSavedTimelineAnchor } from "./use-timeline-window";

// meiva:note
// An applied reading bookmark keeps re-asserting its saved anchor offset while the rows around it settle, and no position is recorded while a restore is pending or unsettled. The guard releases when a layout change observes the anchor at its saved offset, when the entry shows different content, or when the reader takes over (wheel, key, touch, scrollbar, send); a switch away inside the settle therefore keeps the saved bookmark.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/continuous-segments.meiva.okf.md
export function settleSavedTimelineScroll(
  viewport: HTMLElement,
  saved: SavedScroll,
  compactBuffer: CompactTransitionBuffer | null,
  canonicalItems: readonly BackendTimelineItem[],
): boolean {
  if (saved.followBottom || !saved.anchor) return true;
  const target = resolveSavedTimelineAnchor(viewport, saved.anchor, compactBuffer, canonicalItems);
  if (!target.node) return !target.present;
  const offset = target.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
  const delta = offset - target.viewportTop;
  if (Math.abs(delta) <= 1) return true;
  materializeBeforeAnchor(viewport, target.node);
  viewport.scrollTop = Math.max(0, viewport.scrollTop + delta);
  return false;
}
