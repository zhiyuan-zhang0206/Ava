# Browser dependencies at enrollment and install

## Decision

Treat Node.js (`npx`) as an install-time dependency for every agent-runner and
attempt the shared provisioner during enrollment when the browser's other host
requirements already hold. Enrollment remains successful on a headless or
otherwise incapable host, but it emits the same prominent, actionable warning
used by converge so `ava-browser` cannot be silently skipped indefinitely.

## Rationale

The shared headed browser runs `chrome-devtools-mcp` through `npx`. A runner
enrolled without Node.js could successfully enroll and then skip the browser on
every converge with only a small stderr note. The browser dependency check must
therefore be settings-free: enrollment runs before `Settings` can be built and
must detect the platform Chrome default rather than consume a configuration
override.

## Consequences

The capability contract has two explicit callers: settings-aware runtime paths
use `browser_incapability()`, while fresh-host enrollment and dependency repair
use `browser_deps_incapability()`. Both report the same display, Chrome, and
`npx` reasons in the same order. An explicit `AVA_BROWSER_ENABLED=false` remains
an operator choice and suppresses enrollment repair and warnings.

## Update

Display-missing hosts receive an informational not-applicable notice rather than
a repair warning: a headed browser cannot run there, so the service stays
skipped by design and installing Node.js would not help. macOS provisioning now
prefers the linked `node` formula; its keg-only `node@22` fallback is force-linked
so `npx` reaches PATH.
