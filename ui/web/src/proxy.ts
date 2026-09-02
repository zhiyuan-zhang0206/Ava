import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import {
  browserFacingRequestUrl,
  buildContentSecurityPolicy,
  gatewayOriginForRequest,
} from "@/lib/content-security-policy";

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const contentSecurityPolicy = buildContentSecurityPolicy({
    nonce,
    gatewayOrigin: gatewayOriginForRequest(browserFacingRequestUrl(request), {
      apiBase: process.env.NEXT_PUBLIC_API_BASE,
      gatewayPort: process.env.NEXT_PUBLIC_GATEWAY_PORT,
    }),
    isDevelopment: process.env.NODE_ENV === "development",
  });

  // Next.js reads CSP from the forwarded request to attach this nonce to its
  // framework scripts. The browser receives the identical response policy.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", contentSecurityPolicy);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
