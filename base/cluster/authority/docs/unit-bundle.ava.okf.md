---
type: doc
title: Unit bundle exposure
description: What a sealed unit capability bundle carries and who else holds each part.
tags: [authority, lifecycle]
---

# Unit bundle exposure

An `issue-unit` bundle is sealed for one unit, but nothing it carries is that
unit's own:

| Carried | Shared by |
|---|---|
| runner login (role and password) | every runner unit |
| runner API token | every runner unit |
| gateway API token digest | nobody: a digest admits nothing |
| telemetry token | every unit, until the human secret rotates |

A stolen bundle with its transport key, like a compromised unit, therefore
holds the write generation's runner admission: the `ava_runner` database privileges;
a runner API token that the gateway API (a browser login included),
`/api/bootstrap` (the Redis runtime URL and provider keys) and every unit's
`/ops` of that generation admit; and the OTLP relay ingress. Its expiry is the
installer's check, not the cipher's, and nothing records its use: until it
expires it installs on any unit that names itself that machine and home.

The machine binding is a guard against mistakes, not against theft:
`install_bundle` compares the bundle's machine and home with the installing
unit's own `ava init --machine-name` and home, which that unit asserts
about itself, and the credentials work without any installation.

The write generation is the home's only one and nothing replaces it, so a bundle
and its key stay inside the operator's channel; the secrets the bundle reached
beyond the generation rotate on their own: the telemetry token with the human
secret (`scripts/data_plane_ops/rotate_cluster_secret.py`), the Redis runtime
password bootstrap served with
`scripts/data_plane_ops/rotate_data_plane_secrets.py --scope runner`, and the
provider keys at each provider. Why nothing rotates the generation:
[decision](../../../../decisions/2026-10-03-retire-write-generation-rotation.md).
