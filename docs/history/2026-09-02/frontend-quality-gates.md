# Frontend quality gates

The frontend now has component smoke coverage for the fleet, alerts redirect,
Guide entry, and fleet-agent cache reader. Three deterministic Playwright
visual contracts cover the desktop home, desktop fleet, and mobile home views.

Visual references are generated on Ubuntu Chromium in two CI rounds: the first
failing run uploads its candidates, then reviewed PNGs are committed before the
second run. This avoids treating a developer workstation's renderer as a CI
baseline.

The stale Stryker configuration and its unused dependencies were removed.
`@next/bundle-analyzer` is available through `npm run build:analyze`; CI also
enforces a 1,250,000-byte uncompressed home-route first-load JavaScript budget,
set from a 1,207,268-byte measurement. The frontend line budget now mirrors
the backend's 500-line soft / 800-line hard policy, with the current seventeen
soft warnings treated as the allowed baseline.

Update: the fixed warning-count threshold was replaced with a committed
file/rule/line baseline. ESLint still rejects every error and now rejects only
warning identities absent from that baseline, so known soft line-budget
outliers remain visible without unrelated merge-reference warning drift
blocking later frontend changes.
