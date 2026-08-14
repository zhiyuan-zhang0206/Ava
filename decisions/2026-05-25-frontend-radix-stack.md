# Frontend component stack: shadcn / Radix / Tailwind mainstream

## Context

Headless UI component libraries are where the frontend's interactive
primitives live — popovers, dropdowns, separators, scroll areas, buttons.
The hard constraint is real iOS WebKit: iOS Chrome is forced onto WebKit by
the platform, and that is the demographic where headless libraries either
survive years of touch-event shake-out or accumulate a graveyard of mobile
bugs. A library that works everywhere else can still fail there.

A second constraint surfaced from the same failure: **emulation does not
reliably reproduce iOS WebKit touch bugs**. Playwright `webkit` with an
iPhone device descriptor accepts fixes that the real device still fails.
So the verification path for any mobile-touch behavior must end on a real
device, not in emulation.

## Decision

Stay on the **shadcn / Radix / Tailwind** mainstream for frontend
components:

- Interactive primitives (Popover, Separator, ScrollArea, and the like) →
  **Radix UI**.
- Buttons, inputs, and other simple elements → plain HTML elements styled
  with Tailwind via the existing `buttonVariants(...)` cva, not a library
  primitive.

The governing rule: **do not adopt "interesting" headless component
libraries until they have at least a year of real mobile-WebKit shake-out.**
Novelty is not a selection criterion; battle-testing on iOS WebKit is.

Keep the mobile e2e test (Playwright webkit + iPhone descriptor) — it catches
the subset of bugs that fail even in emulation, which is non-zero useful — but
it cannot replace a real-device smoke check before declaring a mobile-touch
fix landed.

## Alternatives rejected

**`@base-ui/react`** (MUI team, 2024 GA) — rejected. It is the load-bearing
lesson here. A real-device popover failure (mobile picker dropdown that never
appeared on iOS Chrome) was chased through a sequence of plausible, individually
*correct* fixes, each of which the emulator accepted and the real iPhone still
failed:

- `modal=true` on the popover root does block iOS ghost-clicks — but the
  real device still failed.
- The library's known iOS touch bugs were genuinely fixed in a later
  release — upgrading still failed on device.
- Two nested `useButton` hooks on one DOM node (the library's trigger
  primitive plus its button primitive) is a genuinely fragile pattern —
  collapsing it to a plain `<button>` still failed.

Each fix removed a real bug, but they were leaves of a deeper cause: the
library simply has not been through the years of mobile-WebKit hardening that
Radix has. Only ripping `@base-ui/react` out entirely — primitives to Radix,
buttons/inputs to plain styled elements — fixed the device.

## Consequences

- Commits to the mainstream stack and its larger battle-tested surface; gives
  up the appeal of newer, "cleaner" headless libraries until they earn the
  mobile-WebKit track record.
- Buttons/inputs as plain styled elements means no library-provided button
  behavior — accepted, because the cva styling already covers the need and a
  library primitive there only adds fragile nesting.
- Real-device smoke is now part of the definition of done for any
  mobile-touch fix; passing emulation is necessary but not sufficient.
- The selection rule (one year of mobile-WebKit shake-out) deliberately
  trades early access to promising libraries for not re-running this saga.
