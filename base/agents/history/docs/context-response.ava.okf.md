---
type: doc
title: Agent context response
description: Shared context breakdown and model budget projection for gateway and run insights.
tags: [base, agents, context]
---

# Agent context response

`context_response.py` owns the shared response schema and context budget
projection used by the gateway and run insights. Its callers supply a model
catalog and a required `default_model_reader`; the module holds no configuration
owner. Model lookup reads the agent's overlay first and calls the reader only
when the overlay has no model. Each operation reads the current default, then
resolves withdrawal through the catalog before judging capabilities or budgets.

The response retains the request's token categories and recursive prompt
sections. Budget resolution combines the effective model window with the
agent's existing tuning overrides; an unknown window remains a warning and
zero display thresholds. This overlay-only lookup is unchanged; snapshot birth
pin behavior is described in
[Agent observation evidence](../../observation/docs/observation.ava.okf.md).
