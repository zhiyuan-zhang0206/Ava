---
type: doc
title: CI Sampler Event Writer
description: Explicit entry image and event-writer ownership for PR-flow and CI-run sampling.
tags: []
---

# CI Sampler Event Writer

Standalone `pr_flow_export.py` and `runs_export.py` capture their source image
after argument validation and before remote collection. Only actual emission
constructs their writer through `export_process.owned_event_pipeline`: one
CodeVersion and nonexempt ProcessDbGate supply its live database factory.
A missing loaded SHA remains unknown; no dial captures the current checkout.

The actual writer stops within two seconds. Its unfinished receipt is reported;
the telemetry binding retains the actual pipeline for final process draining.
An export error stays primary if writer cleanup also fails, with the original
worker error retained by the pipeline for repeated collection.

The Dev/CI metrics schedule passes its existing ClientSet producer into the
CI-run sampler. The sampler borrows that writer without capturing another image,
creating another gate, or closing the schedule's resources. Both invocation paths
retain the exporter process dimension. Dry runs create neither image nor writer;
failed authoritative collection still aborts before persistence and emission.
