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

Assigning the forwarded hostname alone would retain the listener's port. The
implementation also assigns the parsed port, including the empty default port.

React's dynamic layouts use style attributes. The attribute directive does not
relax the production directive for style elements. The sandboxed reader can
display shared HTTP(S) links through frame-src. Development adds the allowances
tested by the same suite; they are not production guarantees.
