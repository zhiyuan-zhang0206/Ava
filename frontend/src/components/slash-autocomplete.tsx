"use client";

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";

import type { CommandItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_COL } from "@/lib/layout";

/**
 * Fuzzy-match score for filtering commands: higher = better match, -1 = no match.
 * Exact match (100) > prefix match (80) > subsequence with consecutive bonuses.
 */
export function fuzzyScore(query: string, target: string): number {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (t === q) return 100;
  if (t.startsWith(q)) return 80;

  let qi = 0;
  let score = 0;
  let prevMatch = -2; // sentinel so first match gets "consecutive" bonus
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      qi++;
      score += 1;
      if (ti === prevMatch + 1) score += 2; // consecutive bonus
      if (ti === 0) score += 3; // start-of-word bonus
      prevMatch = ti;
    }
  }
  return qi === q.length ? score : -1;
}

/** Imperative handle the Composer drives from its onKeyDown: the dropdown
 *  owns its match list + active index, the Composer just forwards arrow / tab /
 *  enter / escape while it's open so those keys don't double as submit / newline. */
export interface SlashAutocompleteHandle {
  isOpen: () => boolean;
  moveUp: () => void;
  moveDown: () => void;
  /** Select the active match (fires onSelect). Returns false when nothing was
   *  open to select, so the caller can fall through to its normal Enter. */
  selectActive: () => boolean;
  close: () => void;
}

/** The `/name` token the caret sits in: what the dropdown filters on, and the
 *  range a selection replaces. */
export interface SlashToken {
  /** Index of the token's leading `/` in the value. */
  start: number;
  /** Index one past the token's last character. */
  end: number;
  /** The command name being typed — the token without its `/`. Empty for a
   *  bare `/`, which lists every command. */
  query: string;
}

/** The slash token the caret is in, or null when it isn't in one.
 *
 *  A command only triggers as the message's FIRST token (user ruling
 *  #836): the `/` must be the first non-whitespace character of the value.
 *  Leading whitespace is fine (`  /plan`); a slash wedged mid-message —
 *  prose, a path, a later command — never opens the dropdown, and
 *  multi-command messages are no longer supported. Null also means the
 *  caret isn't on a `/...` run — plain prose, or the instruction that
 *  follows a command name (whitespace ended that token), which is what
 *  closes the dropdown once a name is committed.
 *
 *  Matching is on the WHOLE token, not just the part before the caret, so a
 *  caret parked mid-name still completes that name (`/pl|an` → `plan`) and a
 *  selection replaces the token without eating the rest of it. The cost is
 *  that text glued to the caret from the right belongs to the query
 *  (`/pl|hello` → `plhello`): that won't match anything, so the dropdown stays
 *  shut rather than silently swallowing text on selection. Path-shaped tokens
 *  (`/etc/hosts`) fall out through the same no-match path. */
export function parseSlash(value: string, caret: number): SlashToken | null {
  const c = Math.min(Math.max(caret, 0), value.length);
  let start = c;
  while (start > 0 && !/\s/.test(value[start - 1])) start--;
  if (value[start] !== "/") return null;
  // First-token rule (#836): anything before the `/` other than whitespace
  // means this is not the message's leading command.
  if (value.slice(0, start).trim() !== "") return null;
  let end = c;
  while (end < value.length && !/\s/.test(value[end])) end++;
  return { start, end, query: value.slice(start + 1, end) };
}

interface Props {
  /** The full command list (owned + fetched by the Composer, which also reads
   *  it to surface the committed command's instruction hint — value-derived, so
   *  it covers hand-typed names too). The dropdown is purely presentational
   *  over this. */
  commands: CommandItem[];
  /** The command name being typed in the caret's `/name` token (see
   *  `parseSlash`). `null` when the caret isn't in such a token — plain prose
   *  or a command's instruction — which closes the dropdown. Empty string
   *  ("/" alone) lists every command. */
  query: string | null;
  onSelect: (cmd: CommandItem) => void;
  /** Fired on open-state transitions. The Composer uses it to keep its upward
   *  popovers mutually exclusive (dropdown opening closes the context panel). */
  onOpenChange?: (open: boolean) => void;
}

export const SlashAutocomplete = forwardRef<SlashAutocompleteHandle, Props>(
  function SlashAutocomplete({ commands, query, onSelect, onOpenChange }, ref) {
    const [active, setActive] = useState(0);
    const [dismissed, setDismissed] = useState(false);
    const listRef = useRef<HTMLDivElement>(null);

    const matches = useMemo(() => {
      if (query === null) return [];
      const q = query.toLowerCase();
      const scored = commands
        .map((c) => ({ cmd: c, score: fuzzyScore(q, c.name) }))
        .filter((s) => s.score >= 0)
        .sort((a, b) => b.score - a.score);
      return scored.map((s) => s.cmd);
    }, [query, commands]);

    // Each keystroke (query change) resets the highlight to the top and undoes
    // a previous Esc dismissal — typing again should reopen the dropdown.
    useEffect(() => {
      setActive(0);
      setDismissed(false);
    }, [query]);

    const open = query !== null && !dismissed && matches.length > 0;

    // Notify the owner of open-state *transitions* only (a ref de-dupes the
    // effect re-runs), so its handler can treat every call as an edge.
    const prevOpen = useRef(false);
    useEffect(() => {
      if (prevOpen.current === open) return;
      prevOpen.current = open;
      onOpenChange?.(open);
    }, [open, onOpenChange]);

    // Keep the highlighted option in view as arrow keys walk a list taller than
    // the dropdown's max height — without this the selection scrolls out of
    // sight and the keyboard appears dead (you'd have to reach for the mouse).
    useEffect(() => {
      if (!open) return;
      const el = listRef.current?.children[active];
      el?.scrollIntoView({ block: "nearest" });
    }, [active, open]);

    useImperativeHandle(
      ref,
      () => ({
        isOpen: () => open,
        moveUp: () => setActive((i) => (i - 1 + matches.length) % matches.length),
        moveDown: () => setActive((i) => (i + 1) % matches.length),
        selectActive: () => {
          if (!open) return false;
          const cmd = matches.at(active);
          if (!cmd) return false;
          onSelect(cmd);
          return true;
        },
        close: () => setDismissed(true),
      }),
      [open, matches, active, onSelect],
    );

    if (!open) return null;

    return (
      <div
        ref={listRef}
        role="listbox"
        aria-label="Commands"
        data-testid="slash-autocomplete"
        className="absolute bottom-full left-0 right-0 mb-2 z-50 max-h-[40vh] overflow-y-auto rounded-md border border-border bg-popover shadow-md"
      >
        {matches.map((cmd, i) => (
          <button
            type="button"
            role="option"
            key={cmd.name}
            aria-selected={i === active}
            data-testid={`slash-option-${cmd.name}`}
            // Pointer-down (not click) so selection fires before the textarea's
            // blur — a click would first blur the input and could race the
            // composer's state.
            onMouseDown={(e) => {
              e.preventDefault();
              onSelect(cmd);
            }}
            onMouseEnter={() => setActive(i)}
            className={cn(
              "w-full items-start gap-0.5 px-3 py-2 text-left text-sm",
              i === active ? "bg-accent text-accent-foreground" : "hover:bg-accent/50",
              FLEX, FLEX_COL
            )}
          >
            <span className="font-mono">
              /{cmd.name}
              {cmd.instruction_hint ? (
                <span className="ml-2 text-xs text-muted-foreground">{cmd.instruction_hint}</span>
              ) : null}
            </span>
            {cmd.description ? (
              <span className="text-xs text-muted-foreground">{cmd.description}</span>
            ) : null}
          </button>
        ))}
      </div>
    );
  },
);
