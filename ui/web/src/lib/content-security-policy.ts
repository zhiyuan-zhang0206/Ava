export { gatewayOriginForRequest } from "./gateway-origin";

interface ContentSecurityPolicyOptions {
  nonce: string;
  gatewayOrigin: string;
  isDevelopment: boolean;
}

function firstHeaderValue(value: string | null): string | undefined {
  const firstValue = value?.split(",", 1)[0].trim();
  return firstValue === "" ? undefined : firstValue;
}

function httpOrigin(urlValue: string): string {
  const url = new URL(urlValue);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Gateway URL must use http or https");
  }
  if (url.username !== "" || url.password !== "") {
    throw new Error("Gateway URL must not include credentials");
  }
  return url.origin;
}

function webSocketOrigin(gatewayOrigin: string): string {
  const url = new URL(gatewayOrigin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.origin;
}

/** Return the origin the browser used, including a reverse proxy's forwarding. */
export function browserFacingRequestUrl(request: Request): URL {
  const requestUrl = new URL(request.url);
  const protocol = firstHeaderValue(request.headers.get("x-forwarded-proto"))?.toLowerCase();
  if (protocol === "http" || protocol === "https") requestUrl.protocol = `${protocol}:`;

  // The gate proxies to a loopback Next.js address, so deployment must forward
  // the browser's host as X-Forwarded-Host (and protocol when TLS terminates
  // there). API_BASE uses that browser-facing origin too.
  const browserHost =
    firstHeaderValue(request.headers.get("x-forwarded-host")) ?? request.headers.get("host");
  if (browserHost) {
    const browserOrigin = new URL(`${requestUrl.protocol}//${browserHost}`);
    requestUrl.hostname = browserOrigin.hostname;
    // Assigning .host without a port retains the loopback Next.js port.
    // Explicitly clear it when the proxy exposes the default HTTPS port.
    requestUrl.port = browserOrigin.port;
  }
  return requestUrl;
}

/** Build the per-response CSP consumed by Next.js while it renders the request. */
export function buildContentSecurityPolicy({
  nonce,
  gatewayOrigin,
  isDevelopment,
}: ContentSecurityPolicyOptions): string {
  const resolvedGatewayOrigin = httpOrigin(gatewayOrigin);
  const scriptSources = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"];
  if (isDevelopment) scriptSources.push("'unsafe-eval'");

  const styleSources = isDevelopment
    ? ["'self'", "'unsafe-inline'"]
    : ["'self'", `'nonce-${nonce}'`];

  return [
    "default-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    `script-src ${scriptSources.join(" ")}`,
    `style-src ${styleSources.join(" ")}`,
    // React's dynamic layout values render as style attributes. Limit the
    // compatibility exception to those attributes; nonce-only style elements
    // remain protected by style-src in production.
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: http: https:",
    "font-src 'self' data:",
    `connect-src 'self' ${resolvedGatewayOrigin} ${webSocketOrigin(resolvedGatewayOrigin)}`,
    // Grafana and plugin pages are mounted through the configured gateway.
    `frame-src 'self' ${resolvedGatewayOrigin}`,
    "frame-ancestors 'none'",
  ].join("; ");
}
