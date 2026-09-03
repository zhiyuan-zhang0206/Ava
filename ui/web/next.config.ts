import type { NextConfig } from "next";
import withBundleAnalyzer from "@next/bundle-analyzer";

// No rewrites proxy for /api — the Turbopack dev proxy buffers SSE, so the
// frontend connects directly to FastAPI. Same-origin reverse proxy in prod
// also doesn't need it.
//
// allowedDevOrigins: Next.js 16 dev mode only allows localhost / 127.0.0.1
// by default. A cross-origin dev request (a device on the private network hitting the host,
// phone port-forward, etc.) gets blocked, the JS bundle fails to load and the
// page becomes static HTML — every onClick is unresponsive. Set AVA_DEV_ORIGINS
// (comma-separated MagicDNS hostnames / IPs) to allowlist them. Only `next dev`
// reads this; the prod `next start` the ava stack runs ignores it, so it is
// empty by default.

const nextConfig: NextConfig = {
  // Release preparation traces runtime dependencies before maintenance. Ordinary
  // development/source installs retain their existing next-start output.
  output: process.env.AVA_FRONTEND_RELEASE === "1" ? "standalone" : undefined,
  // -- Security hardening --
  // Strip X-Powered-By header, don't expose the stack
  poweredByHeader: false,

  // script tag crossOrigin="anonymous" — lets the browser attach full
  // stack traces to cross-origin script errors (without leaking cookies)
  crossOrigin: "anonymous",

  // -- Type safety --
  // Enable static type checking for Link href (project is fully TypeScript)
  typedRoutes: true,

  // -- Production security --
  // Explicitly disable production source maps (already the default; declared to prevent accidents)
  productionBrowserSourceMaps: false,

  // -- Dev origin allowlist (env-driven; see note above) --
  allowedDevOrigins: (process.env.AVA_DEV_ORIGINS ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),

  // -- Legacy route redirect --
  // /settings was renamed to /control (the page outgrew "settings": it carries
  // live cluster status and operational actions, not just preferences).
  // Redirect old bookmarks/links; not `permanent` so a browser doesn't cache it
  // past a future adjustment. URL fragments (#status, #skills, …) are
  // client-side only and survive the redirect unmodified — no rule needed for
  // them.
  async redirects() {
    return [
      { source: "/settings", destination: "/control", permanent: false },
      { source: "/settings/:path*", destination: "/control/:path*", permanent: false },
    ];
  },

  // -- Security response headers --
  async headers() {
    return [
      {
        // all routes
        source: "/(.*)",
        headers: [
          // anti clickjacking
          { key: "X-Frame-Options", value: "DENY" },
          // anti MIME sniffing
          { key: "X-Content-Type-Options", value: "nosniff" },
          // control Referer disclosure granularity
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          // CSP needs a unique nonce for every response, which `headers()`
          // cannot provide. src/proxy.ts emits it after deriving the gateway origin
          // from the same NEXT_PUBLIC_* deployment settings as the API client.
        ],
      },
    ];
  },
};

export default withBundleAnalyzer({
  // Webpack reports are generated only for the opt-in `npm run build:analyze`.
  enabled: process.env.ANALYZE === "true",
  analyzerMode: "static",
  openAnalyzer: false,
})(nextConfig);
