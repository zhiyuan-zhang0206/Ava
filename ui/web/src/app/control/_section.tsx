"use client";

// One section of the vertical Control page: a stable anchor (`id` = URL hash +
// scroll target), an always-rendered heading (so the section is Ctrl-F- and
// deep-link-reachable), and a visibility provider that fetches its content from
// first paint and pauses only its polling once scrolled out of view — see
// _visibility.tsx.

import { type ReactNode } from "react";

import { SectionVisibilityProvider, useInView } from "./_visibility";

export function ControlSection({
  id,
  label,
  description,
  children,
}: {
  id: string;
  label: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  const [ref, inView] = useInView();
  return (
    // scroll-mt keeps the heading off the very top edge when jumped to by anchor.
    <section id={id} ref={ref} className="scroll-mt-4">
      <header className="mb-3 border-b border-border pb-1">
        <h2 className="text-lg font-semibold text-foreground">{label}</h2>
        {description ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        ) : null}
      </header>
      <SectionVisibilityProvider visible={inView}>{children}</SectionVisibilityProvider>
    </section>
  );
}
