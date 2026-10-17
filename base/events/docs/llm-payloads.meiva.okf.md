---
type: doc
title: LLM event payload semantics
description: Usage-time pricing, provider error identity and completed-compaction attribution.
tags: [contracts]
---

# LLM event payload semantics

## Usage time price snapshot

Rates are USD per million tokens. Optional cache-write subdivisions and their TTL-specific rates retain the call-time input cost breakdown. `calls` provides a per-agent/model counter independently of histograms whose views drop agent identity. Typed payload keys also bound queryable attributes; undeclared keys must not become query fragments.

## Provider error dimensions

Every classified failure, including a retried transient, is observable; `fatal` describes whether it aborted the turn. `vendor` is the model-provider key and `model` is the model in force. `provider` identifies the SDK package raising the exception, which multiple vendors may share; it cannot identify whose billing key needs attention.
