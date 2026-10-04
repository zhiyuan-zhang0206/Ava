---
type: doc
title: Diagnostic episode reporting
description: Root diagnostic state distinguishes failed observations, unknown evidence, and recovered episodes.
tags:
- ops
---

# Diagnostic episode reporting

`DiagnosticMonitor` stores the latest result and timestamp for each explicitly
registered check. No sample is represented by null fields, not a healthy default.
Repeated observations of one verdict do not repeat its transition event. Browser
failure thresholds count only observed DOWN results; unavailable evidence resets
the count and cannot resolve an external station outage.

Fresh observations feed bounded report callbacks. The permissions helper emits
one unhealthy event until a healthy ping rearms it. Station probes retain their
existing alert transition logic. Loki distinguishes write failure from admission
throttling; sustained throttling emits at the third observation and every 30th
thereafter. Reporting cannot acquire a service restart or OS-job capability.
