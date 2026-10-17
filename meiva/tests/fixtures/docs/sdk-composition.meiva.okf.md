---
type: doc
title: SDK fixture composition
description: Current ownership boundaries and their executable evidence.
---

# SDK fixture composition

The SDK environment fixture supplies one prepared configuration, lazy client set, machine identity and live clock factory per test. A test's bare home does not replace the prepared configuration. Repinning identity retains existing clients and the clock factory unless explicitly replaced, so an already-entered client override remains active. Identity restoration and metering cleanup finish before clients close; cleanup restores the prior process context. Hosted turn resources remain owned until the service fixture joins them.

Identity helpers are separate from the autouse restoration plugin because direct helper imports must not pre-import a pytest plugin before assertion rewriting. Metering cleanup removes only the installation's own outer recorders.

The bound SDK reads its context clock owner and refuses a context without one instead of borrowing the installation clock.
