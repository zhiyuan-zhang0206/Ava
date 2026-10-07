"use client";

// CSP nonce context — the per-response nonce minted in proxy.ts and read from
// the x-nonce request header by the root layout. Providers publishes it here
// so client components can attach it to style elements they render
// themselves; the production style-src only applies inline styles that carry
// it (see content-security-policy.ts). First consumer: ScrollArea forwarding
// it to Radix's injected native-scrollbar-hide <style> (task #3264).

import { createContext, useContext, type ReactNode } from "react";

const NonceContext = createContext<string | undefined>(undefined);

export function NonceProvider({ value, children }: { value?: string; children: ReactNode }) {
  return <NonceContext.Provider value={value}>{children}</NonceContext.Provider>;
}

export function useNonce(): string | undefined {
  return useContext(NonceContext);
}
