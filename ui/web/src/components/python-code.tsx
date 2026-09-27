"use client";

// Python code block with syntax highlighting, a copy button, and an optional
// streaming cursor on the last line.
//
// React.memo wraps the whole component — during streaming the timeline
// re-renders often, but as long as `code` / `streaming` props don't
// change we skip re-highlighting (Prism tokenization is the main CPU
// cost of this component). React's default shallow comparator handles
// string + bool, so no custom areEqual is needed.

import { memo, useEffect, useState } from "react";
import type { Highlight as HighlightComponentType, PrismTheme } from "prism-react-renderer";

import { CopyButton } from "@/components/copy-button";

// prism-react-renderer is ~85KB uncompressed — too large to ship in the "/"
// route's initial bundle (ui/web/scripts/check-first-load-js.mjs budget).
// Lazy-load it via a plain dynamic import (still its own webpack chunk, kept
// out of first load) rather than next/dynamic, so this module fully controls
// the loading state below instead of flashing nothing while the chunk is
// in flight.
//
// The same `loadHighlight` call is reused as the prefetch trigger
// (preloadPythonCodeHighlighter, called from CodeHighlighterPreloader on
// idle and from pointer-enter/focus "intent" on a code block's collapsed
// header — see card.tsx / schedules page). By the time a user actually
// expands a block the chunk is almost always already resolved, so the
// `useState` initializer below picks up the cached component on the very
// first render — no flash of unhighlighted content. When it isn't resolved
// yet (a cold render with no prior intent signal), the component falls back
// to plain, unhighlighted text and swaps to the highlighted version once the
// chunk arrives, instead of rendering nothing.
let highlightPromise: Promise<typeof HighlightComponentType> | null = null;
let resolvedHighlight: typeof HighlightComponentType | null = null;

function loadHighlight(): Promise<typeof HighlightComponentType> {
  highlightPromise ??= import("prism-react-renderer").then((mod) => {
    resolvedHighlight = mod.Highlight;
    return mod.Highlight;
  });
  return highlightPromise;
}

/**
 * Starts fetching the syntax-highlighter chunk without waiting for a code
 * block to mount and need it. Safe to call from multiple sites (idle
 * prefetch, pointer/focus intent, the component's own mount) — the
 * underlying dynamic import is cached after the first call, so a later call
 * is a no-op that resolves to the same (possibly already-resolved) module.
 */
export function preloadPythonCodeHighlighter(): Promise<typeof HighlightComponentType> {
  return loadHighlight();
}

/**
 * Test-only: clears the module-level highlighter cache so a test can control
 * whether a render starts warm (prefetched) or cold. Not called by
 * production code — python-code.test.tsx uses it instead of vi.resetModules()
 * + a fresh dynamic re-import, which reinstantiates prism-react-renderer
 * itself (an ESM dependency Vite doesn't externalize) as a second, partially
 * initialized copy and crashes.
 */
export function __resetHighlighterCacheForTests(): void {
  highlightPromise = null;
  resolvedHighlight = null;
}

// The theme references CSS variables — actual colors live in globals.css
// under :root / .dark. next-themes toggles the .dark class on system
// changes; the theme follows automatically without needing JS to
// subscribe to matchMedia.
const theme: PrismTheme = {
  plain: {
    color: "var(--foreground)",
    backgroundColor: "transparent",
  },
  styles: [
    { types: ["keyword"], style: { color: "var(--syntax-keyword)" } },
    { types: ["string", "char"], style: { color: "var(--syntax-string)" } },
    {
      types: ["comment"],
      style: { color: "var(--syntax-comment)", fontStyle: "italic" },
    },
    { types: ["number", "boolean"], style: { color: "var(--syntax-number)" } },
    { types: ["function", "decorator"], style: { color: "var(--syntax-function)" } },
    {
      types: ["builtin", "class-name"],
      style: { color: "var(--syntax-builtin)" },
    },
    {
      types: ["punctuation", "operator"],
      style: { color: "var(--syntax-punct)" },
    },
  ],
};

interface Props {
  code: string;
  streaming?: boolean;
}

export const PythonCode = memo(function PythonCode({ code, streaming = false }: Props) {
  // Lazy initializer reads the module-level cache synchronously at mount
  // time — if a prior prefetch already resolved it, the very first render
  // is already the highlighted tree.
  const [Highlight, setHighlight] = useState<typeof HighlightComponentType | null>(
    () => resolvedHighlight,
  );

  useEffect(() => {
    if (Highlight !== null) return;
    let active = true;
    void loadHighlight().then((Comp) => {
      // setHighlight(Comp) would be misread by React as a functional
      // updater (any function passed to a state setter is called with the
      // previous state instead of stored) — the () => Comp wrapper stores
      // the component itself.
      if (active) setHighlight(() => Comp);
    });
    return () => {
      active = false;
    };
  }, [Highlight]);

  if (Highlight === null) {
    return <PlainCode code={code} streaming={streaming} />;
  }

  return (
    <Highlight theme={theme} code={code} language="python">
      {({ tokens, getLineProps, getTokenProps }) => (
        <div className="group relative">
          <CopyButton text={code} label="code" streaming={streaming} />
          <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed my-2">
            {tokens.map((line, i) => {
              const isLast = i === tokens.length - 1;
              const lineProps = getLineProps({ line });
              return (
                <div key={i} {...lineProps}>
                  {line.map((token, j) => (
                    <span key={j} {...getTokenProps({ token })} />
                  ))}
                  {streaming && isLast ? <BlinkCursor /> : null}
                </div>
              );
            })}
          </pre>
        </div>
      )}
    </Highlight>
  );
});

// Rendered while the highlighter chunk is still loading (no prior prefetch
// caught up in time) — plain, unhighlighted source rather than a blank body,
// so the copy control and the code itself are always available immediately.
function PlainCode({ code, streaming }: Props) {
  return (
    <div className="group relative">
      <CopyButton text={code} label="code" streaming={streaming} />
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed my-2">
        {code}
        {streaming ? <BlinkCursor /> : null}
      </pre>
    </div>
  );
}

function BlinkCursor() {
  return (
    <span className="inline-block w-1.5 h-3 ml-0.5 bg-foreground/70 animate-pulse align-middle" />
  );
}
