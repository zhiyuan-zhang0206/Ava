import type { Metadata } from "next";
import { Inter, Geist_Mono } from "next/font/google";

import { AuthGuard } from "@/components/auth/auth-guard";
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

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
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
        <Providers><AuthGuard>{children}</AuthGuard></Providers>
      </body>
    </html>
  );
}
