import type { NextConfig } from "next";

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

const CSP_DIRECTIVES = [
  "default-src 'self'",
  // script: Next.js inline script + own bundle
  "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  // style: Tailwind + shadcn use inline style
  "style-src 'self' 'unsafe-inline'",
  // img: data: for favicon / inline image, https: for external images
  "img-src 'self' data: http: https:",
  // font: Google Fonts (Geist)
  "font-src 'self' data:",
  // connect: API_BASE (gateway) + SSE EventSource
  "connect-src 'self' http: https: ws: wss:",
  // frame-src: the /insights#ops Grafana embed — the gateway's /grafana
  // reverse proxy on API_BASE. In dev that is a different port than the
  // frontend (cross-origin), so 'self' alone would block it; mirror
  // connect-src's permissiveness.
  "frame-src 'self' http: https:",
  // frame-ancestors: prevent frontend from being embedded externally (X-Frame-Options duplicates this)
  "frame-ancestors 'none'",
  // everything else stays default
].join("; ");

const nextConfig: NextConfig = {
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
          // Content-Security-Policy: defense-in-depth against XSS.
          // 'unsafe-inline' in script-src is currently unavoidable in Next.js
          // (inline script for hydration); style-src 'unsafe-inline' because
          // Tailwind/shadcn use inline style heavily. Consider nonce-based
          // tightening later.
          { key: "Content-Security-Policy", value: CSP_DIRECTIVES },
        ],
      },
    ];
  },
};

export default nextConfig;
