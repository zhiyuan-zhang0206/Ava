// Pure layout for the raw-context strip (P4-2, task #4023) — the bottom-most
// chart row showing every context message in the window, width proportional
// to its character count. A faithful port of the pilot demo's `lay()` two-pass
// packing (pilot-delivery/timeline.html), expressed once here as a tested
// pure function:
//
//   X_k = max(t_k, E_{k-1})          t_k = time position px, E running end
//   E_k = X_k + chars_k * scale      scale = W^2 / max_j(t_j * C + S_j * W)
//
// (C = total chars, S_j = suffix chars from j — the demo's `pack` recurrence,
// resolved in one pass.) The demo's axis grew past the time span to fit the
// tail; our axis is the response window, so the chart clips the strip at the
// plot edge instead (the over-extended tail is one zoom away, and squeezing
// every bar to fit would trade the whole strip's readability for the tail).
//
// The wire's ts-less head message (the system prompt, created at birth with
// no ava_created_at) has no time position; the server sorts it first and the
// strip anchors it at the plot start — the head-of-context reading order the
// demo's strip shows.

import type { RunTimelineMessage, RunTimelineResponse } from "@/lib/types";

export interface StripPartLayout {
  kind: RunTimelineMessage["parts"][number]["kind"];
  chars: number;
  left: number;
  width: number;
}

export interface StripMessageLayout {
  messageIndex: number;
  key: string;
  left: number;
  width: number;
  parts: StripPartLayout[];
}

export interface StripLayout {
  messages: StripMessageLayout[];
  /** px-per-char scale actually applied (exposed for tests and readouts). */
  scale: number;
}

export function buildStripLayout(
  messages: RunTimelineMessage[],
  window: RunTimelineResponse["window"],
  plot: { left: number; width: number },
): StripLayout {
  const width = Math.max(1, plot.width);
  const from = Date.parse(window.from);
  const to = Date.parse(window.to);
  if (messages.length === 0 || !(to > from)) {
    return { messages: [], scale: 0 };
  }
  const times = messages.map((message) => {
    if (message.ts === null) return 0;
    const parsed = Date.parse(message.ts);
    if (!Number.isFinite(parsed)) {
      throw new Error(`run-timeline strip: unparsable message timestamp ${message.ts}`);
    }
    return Math.max(0, Math.min(width, ((parsed - from) / (to - from)) * width));
  });
  const chars = messages.map((message) => Math.max(0, message.chars));
  const totalChars = chars.reduce((sum, value) => sum + value, 0);

  let scale = 0;
  if (totalChars > 0) {
    // The demo's `pack`, unrolled: the packed end of the unit-width greedy
    // layout (px + chars mixed units) is max_j (t_j * C + S_j * W), and the
    // scale that lands the packed extent on the plot width is W^2 / pack.
    let pack = 0;
    let suffixChars = totalChars;
    times.forEach((time, index) => {
      pack = Math.max(pack, time * totalChars + suffixChars * width);
      suffixChars -= chars[index];
    });
    scale = (width * width) / pack;
  }

  let runningEnd = 0;
  const stripMessages: StripMessageLayout[] = messages.map((message, index) => {
    const left = Math.max(times[index], runningEnd);
    const barWidth = chars[index] * scale;
    runningEnd = left + barWidth;
    // Part sub-rects use the same px-per-char scale, so a bar's internal
    // split is exactly proportional to each part's characters.
    let partCursor = left;
    const parts = message.parts.map((part) => {
      const partWidth = Math.max(0, part.chars) * scale;
      const layout = {
        kind: part.kind,
        chars: part.chars,
        left: plot.left + partCursor,
        width: partWidth,
      };
      partCursor += partWidth;
      return layout;
    });
    return {
      messageIndex: index,
      key: message.key,
      left: plot.left + left,
      width: barWidth,
      parts,
    };
  });

  return { messages: stripMessages, scale };
}

/** Linear char-domain layout for the context axis (P4-2b): every message
 *  takes exactly its character slot, end to end, and the viewport maps
 *  [view.from, view.to] onto the plot. The demo's `lay()` else-branch, with
 *  our local viewport in place of its full-axis extent; no refetch is
 *  involved — the caller passes plain character numbers. Geometry may fall
 *  outside the plot (the SVG clip cuts it; buttons clamp), so widths stay
 *  exactly char-proportional. */
export function buildContextStripLayout(
  messages: RunTimelineMessage[],
  view: { from: number; to: number },
  plot: { left: number; width: number },
): StripLayout {
  const width = Math.max(1, plot.width);
  const span = view.to - view.from;
  if (messages.length === 0 || !(span > 0)) {
    return { messages: [], scale: 0 };
  }
  const scale = width / span;
  const toX = (char: number) => plot.left + (char - view.from) * scale;
  let offset = 0;
  const stripMessages: StripMessageLayout[] = messages.map((message, index) => {
    const chars = Math.max(0, message.chars);
    const left = toX(offset);
    const barWidth = chars * scale;
    let partCursor = left;
    const parts = message.parts.map((part) => {
      const partWidth = Math.max(0, part.chars) * scale;
      const layout = {
        kind: part.kind,
        chars: part.chars,
        left: partCursor,
        width: partWidth,
      };
      partCursor += partWidth;
      return layout;
    });
    offset += chars;
    return { messageIndex: index, key: message.key, left, width: barWidth, parts };
  });
  return { messages: stripMessages, scale };
}

/** Message indexes whose stamp falls inside a layer node's [start, end] —
 *  the demo's span test, here on timestamps. The ts-less head message is
 *  never "covered" by a summary node. */
export function coveredMessageIndexes(
  messages: RunTimelineMessage[],
  nodeStart: string,
  nodeEnd: string,
): number[] {
  const start = Date.parse(nodeStart);
  const end = Date.parse(nodeEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end) || !(end > start)) return [];
  return messages.flatMap((message, index) => {
    if (message.ts === null) return [];
    const stamp = Date.parse(message.ts);
    return Number.isFinite(stamp) && stamp >= start && stamp <= end ? [index] : [];
  });
}

/** The summary-node chain covering a message — the demo's chainFor(): the
 *  leaf is the deepest (then narrowest) covering node, and the chain is its
 *  ancestors followed by the leaf itself. Empty when nothing covers it (a
 *  ts-less head message, or a window without summary nodes). */
export function messageChainIndexes(
  layers: NonNullable<RunTimelineResponse["layers"]> | null | undefined,
  ts: string | null,
): number[] {
  if (!layers || layers.length === 0 || ts === null) return [];
  const stamp = Date.parse(ts);
  if (!Number.isFinite(stamp)) return [];
  const byId = new Map(layers.map((node, index) => [node.id, index]));
  const covering = layers
    .map((node, index) => ({ node, index }))
    .filter(({ node }) => Date.parse(node.start) <= stamp && stamp <= Date.parse(node.end));
  if (covering.length === 0) return [];
  const leaf = covering.reduce((best, candidate) => {
    const span = Date.parse(candidate.node.end) - Date.parse(candidate.node.start);
    const bestSpan = Date.parse(best.node.end) - Date.parse(best.node.start);
    if (candidate.node.depth > best.node.depth) return candidate;
    if (candidate.node.depth === best.node.depth && span < bestSpan) return candidate;
    return best;
  });
  const chain: number[] = [leaf.index];
  let parentId = leaf.node.parent;
  while (parentId !== null) {
    const parentIndex = byId.get(parentId);
    if (parentIndex === undefined) break;
    chain.unshift(parentIndex);
    parentId = layers[parentIndex].parent;
  }
  return chain;
}
