---
type: doc
title: Browser origin and content security policy
description: Forwarded browser origins and the production script, style and connection directives.
tags: [frontend, security, contracts]
---

# Browser origin and content security policy

The gate connects to a loopback Next.js listener. It must forward the browser's
host and protocol so origin derivation uses the public address. The URL helper
uses the first forwarded value, accepts HTTP or HTTPS protocol values and falls
back to Host when X-Forwarded-Host is absent.

```meiva-contract
id = "forwarded-origin"
kind = "invariant"
statement = "A forwarded HTTPS host with no explicit port clears the loopback listener's port. Forwarded HTTP protocol matching is case-insensitive."
code = ["ui/web/src/lib/auth/content-security-policy.ts:browserFacingRequestUrl"]
tests = [
  "ui/web/src/lib/auth/content-security-policy.test.ts::gatewayOriginForRequest::matches the configured HTTP/2 entry after proxy forwarding",
  "ui/web/src/lib/auth/content-security-policy.test.ts::browserFacingRequestUrl::normalizes an uppercase forwarded protocol",
]
```

Assigning the forwarded hostname alone would retain the listener's port. The
implementation also assigns the parsed port, including the empty default port.

```meiva-contract
id = "production-directives"
kind = "invariant"
statement = "Production script policy uses a nonce and strict-dynamic without unsafe-eval or unsafe-inline. Style elements require the nonce while style attributes allow unsafe-inline. Connections are limited to the current page's own origin and the gateway HTTP and WebSocket origins."
code = ["ui/web/src/lib/auth/content-security-policy.ts:buildContentSecurityPolicy"]
tests = [
  "ui/web/src/lib/auth/content-security-policy.test.ts::buildContentSecurityPolicy::uses a nonce instead of unsafe script execution in production",
  "ui/web/src/lib/auth/content-security-policy.test.ts::buildContentSecurityPolicy::limits connections to the gateway HTTP and WebSocket origins",
]
```

React's dynamic layouts use style attributes. The attribute directive does not
relax the production directive for style elements. The sandboxed reader can
display shared HTTP(S) links through frame-src. Development adds the allowances
tested by the same suite; they are not production guarantees.
