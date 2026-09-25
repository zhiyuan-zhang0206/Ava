import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Inter, Geist_Mono } from "next/font/google";
import { connection } from "next/server";

import { AuthGuard } from "@/components/auth/auth-guard";
import { VisualViewportHeightSync } from "@/components/visual-viewport-height-sync";
import { Providers } from "@/components/providers";
import "./globals.css";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Ava",
  description: "Ava agent web UI",
};

// Declares both schemes the console supports; the browser follows
// prefers-color-scheme for UA-rendered parts (scrollbars, form controls)
// when the app has not set its own. The explicit `color-scheme` rules in
// globals.css track the ACTIVE theme (light/dark class), so they take
// precedence over this static hint and keep native widgets in step with
// what the palette shows (task #2695 — Dark Reader reads the same signals).
export const viewport: Viewport = {
  colorScheme: "dark light",
  // Android Chrome shrinks the LAYOUT viewport (and with it the h-full shell)
  // for the on-screen keyboard when asked to; the default `resizes-visual`
  // would leave the composer under the keyboard. iOS Safari ignores this meta
  // — the `VisualViewportHeightSync` component below is the iOS half of the
  // same contract (task #4779).
  interactiveWidget: "resizes-content",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // A nonce exists only for a live request. Waiting for one prevents Next from
  // prerendering framework scripts that cannot receive the request nonce.
  await connection();
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    // suppressHydrationWarning on <html>: next-themes injects an inline
    // script that modifies the <html> class before hydration. Server
    // render has no .dark; the client's first frame does — expected, so
    // it must be suppressed.
    //
    // Note: suppressHydrationWarning only silences the element it's on,
    // not its descendants; Kapture / other devtools class injection on
    // <body> still needs its own suppression on <body>.
    // lang is corrected client-side by LanguageProvider once the
    // display.language setting loads (the shell can't read it during SSR).
    <html
      lang="en"
      suppressHydrationWarning
      className={`${inter.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body
        // Devtools extensions like Kapture add classes (e.g.
        // `kapture-loaded`) to body before React hydrates — not a bug,
        // suppress separately.
        suppressHydrationWarning
        className={cn("min-h-full h-full bg-background text-foreground", FLEX, FLEX_COL)}
      >
        {/* On-screen keyboard contract (task #4779): the Android half is the
            `interactiveWidget` viewport export above; this is the iOS half,
            which Safari needs because it ignores the meta. Renders nothing. */}
        <VisualViewportHeightSync />
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-background focus:px-4 focus:py-2 focus:text-foreground focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-ring"
        >
          Skip to main content
        </a>
        <Providers nonce={nonce}><AuthGuard>{children}</AuthGuard></Providers>
      </body>
    </html>
  );
}
