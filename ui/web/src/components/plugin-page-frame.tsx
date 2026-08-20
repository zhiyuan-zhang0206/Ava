"use client";

// A plugin-served page, embedded.
//
// The iframe is **breakage containment, not a security boundary** — the design
// is explicit about this. Plugin Python already runs inside agent processes
// with shell and DB access, so the trust decision was made at install time (the
// scan gate + trust tier); a plugin page learns nothing its Python half does
// not already have. What the frame buys is that a broken or slow page fails
// alone instead of taking the console with it, and that the console's own
// bundle never contains third-party code.
//
// The sandbox therefore keeps the capabilities a page needs to be a page —
// scripts, its own origin (the gateway's, so it can fetch its own data and use
// storage), forms — and withholds the ones a page has no business taking:
// top-level navigation, downloads, pointer lock, popups.

import { pluginPageSrc } from "@/lib/plugin-nav";
import { cn } from "@/lib/utils";

const SANDBOX = "allow-scripts allow-same-origin allow-forms";

export function PluginPageFrame({
  plugin,
  page,
  title,
  className,
}: {
  plugin: string;
  page: string;
  title: string;
  className?: string;
}) {
  return (
    <iframe
      src={pluginPageSrc(plugin, page)}
      title={title}
      sandbox={SANDBOX}
      // referrerPolicy: the console's own URL is not the plugin's business.
      referrerPolicy="no-referrer"
      className={cn("size-full border-0 bg-background", className)}
    />
  );
}
