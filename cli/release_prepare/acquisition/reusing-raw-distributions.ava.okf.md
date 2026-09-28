---
type: doc
title: Reusing raw release input distributions
description: Exporting a prior acquisition's raw distributions into a private store and reusing them, hash-bound, from a fresh acquisition.
tags:
- release
---

# Reusing raw release input distributions

An optional `source_distributions: TreeInput` in `Acquisition` supplies a
previously captured flat package inventory. Export one from an explicitly
hash-bound successful acquisition receipt into a caller-chosen private store:

```bash
mkdir -m 700 /absolute/release-inputs
.venv/bin/python -m cli.release_prepare.source_distributions \
  --receipt /absolute/acquisition/acquisition-receipt.json \
  --receipt-sha256 CAPTURED_RECEIPT_SHA256 --store /absolute/release-inputs
```

The command returns the ordinary `TreeInput` JSON for the request field.
Choose a store outside disposable preview runs. Export verifies the original
source, tools and derivations, privately copies only raw distributions and
publishes a complete directory named by its inventory hash. Existing objects
must match; partial exports cannot replace them. The copy has no links to its
producing run, which can subsequently be removed.

Every consumer still captures its own commit and exports its own locked
requirements. Pip selects and copies from the seed with `--no-index`,
`--find-links` and required hashes; direct URLs and install directives refuse.
Missing packages or changed bytes fail without a network fallback. A different
source lock may reuse artifacts already present at its exact required hashes.
Source archives are rebuilt privately; the wheel derivations, build caches,
assets, application and image identities remain fresh. No application wheel,
mutable cache or previous image is reused through this input.

The seed is checked before effects, immediately before package selection and
when verifying the completed receipt. It remains a required retained input for
that receipt, independently of the old producing acquisition. Python, build-tool
and frontend acquisition still use their existing cold paths.
