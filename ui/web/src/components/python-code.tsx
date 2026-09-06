"use client";

// Python code block with syntax highlighting, a copy button, and an optional
// streaming cursor on the last line.
//
// React.memo wraps the whole component — during streaming the timeline
// re-renders often, but as long as `code` / `streaming` props don't
// change we skip re-highlighting (Prism tokenization is the main CPU
// cost of this component). React's default shallow comparator handles
// string + bool, so no custom areEqual is needed.

import dynamic from "next/dynamic";
import { memo } from "react";
import type { PrismTheme } from "prism-react-renderer";

import { CopyButton } from "@/components/copy-button";

// prism-react-renderer is ~30KB gzipped — lazy-load it, only fetched
// when the timeline actually has a python code block. While loading,
// fall back to a plain text <pre>.
const Highlight = dynamic(
  () => import("prism-react-renderer").then((mod) => mod.Highlight),
  { ssr: false },
);

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
  return (
    <Highlight theme={theme} code={code} language="python">
      {({ tokens, getLineProps, getTokenProps }) => (
        <div className="group relative">
          <CopyButton text={code} label="code" streaming={streaming} />
          <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-sm leading-relaxed my-2">
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

function BlinkCursor() {
  return (
    <span className="inline-block w-1.5 h-3 ml-0.5 bg-foreground/70 animate-pulse align-middle" />
  );
}
