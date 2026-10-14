import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { saveScrollMemory } from "@/lib/layout/scroll-memory";
import type { DetailsMode } from "@/lib/layout/content-toggle-store";

/** A bookmark always keeps the last read item, including a former bottom follower. */
export function rememberTimelinePosition(viewport: HTMLElement, entryKey: string, contentKey: string) {
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  saveScrollMemory(entryKey, {
    contentKey, scrollTop: viewport.scrollTop, followBottom: false,
    anchor: node?.dataset.itemId ? {
      itemId: node.dataset.itemId, rank: Number(node.dataset.displayRank ?? 0),
      viewportTop: node.getBoundingClientRect().top - rect.top,
    } : undefined,
  });
}

interface ReadingExpansion {
  mode: DetailsMode;
  items: ReadonlySet<string>;
  turns: ReadonlyMap<string, boolean>;
}
// Only ephemeral view choices live here. Server rows stay in the QueryClient.
const expansions = new Map<string, ReadingExpansion>();
export function readTimelineExpansion(key: string | undefined, mode: DetailsMode) {
  const saved = key ? expansions.get(key) : undefined;
  return saved?.mode === mode ? saved : undefined;
}
export function rememberTimelineExpansion(key: string | undefined, value: ReadingExpansion) {
  if (key) expansions.set(key, value);
}


/** Scroll keys and scrollbar drags resume normal following after a bookmark. */
export function watchReadingNavigation(viewport: HTMLElement, wrapper: HTMLElement | null, resume: () => void) {
  const keyDown = (event: KeyboardEvent) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) resume();
  };
  const pointerDown = (event: PointerEvent) => {
    if ((event.target as HTMLElement).closest('[data-slot="scroll-area-scrollbar"]')) resume();
  };
  viewport.addEventListener("keydown", keyDown);
  wrapper?.addEventListener("pointerdown", pointerDown, { capture: true });
  return () => {
    viewport.removeEventListener("keydown", keyDown);
    wrapper?.removeEventListener("pointerdown", pointerDown, { capture: true });
  };
}
