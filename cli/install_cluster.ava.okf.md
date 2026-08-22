---
type: doc
title: Install-Time Cluster Birth
description: '`cli/install_cluster.py` — the ONLY place a cluster is born: registry entry keyed by home path, own pg/redis, provisioned database, `$AVA_HOME/.env`. Run as the final step of `scripts/install.sh`.'
tags:
- cli
- cluster-lifecycle
---

# Install-Time Cluster Birth

## What it is

`scripts/install.sh`'s final step runs `python -m cli.install_cluster`. It is
the **only** path that brings a cluster into existence — `ava start` is a pure
bring-up and refuses an uninstalled home rather than birthing one implicitly.

A born cluster consists of four things, all produced here:

- a registry entry, **keyed by home path** (host-level `~/.ava/clusters.json`)
- the cluster's own pg/redis instance under `$AVA_HOME`
- a provisioned database + owning role
- `$AVA_HOME/.env` — the connection facts; the secret follows the role (below)

The data-plane identity (db / role / ACL user) is the fixed
`shared.cluster.DATA_PLANE_IDENTITY` (`"ava"`), chosen **only here**. Everywhere
else it is read back out of the `.env` URL as data, so nothing re-derives an
identifier from a name.

## The cluster secret (no-auth single box by default)

User decision (2026-08): off is fully off. Secret precedence: the compatibility
`--cluster-secret` flag > the one-shot `AVA_INSTALL_CLUSTER_SECRET` input > the
home's existing `.env` secret (never rotated) > the role default. The role
default: a **single-machine** role
(`gateway,agent-runner`) births a NO-AUTH cluster with an EMPTY secret — every
surface (gateway API, /ops, pg/redis) serves unauthenticated on loopback. A
**gateway-only split host** mints a fresh secret — remote agent-runners depend
on it (scram/requirepass + bearer), so a split deployment always has one. The
generic runtime environment is never consulted (an inherited
`AVA_CLUSTER_SECRET` must not leak in); the dedicated install variable is
consumed before bootstrap and forwarded only to the birth child. The URLs
always carry the identity username, with or without a password.

## Flags

- `--role gateway,agent-runner` — REQUIRED (no default). Sets the serve flags,
  and decides whether to birth at all: only gateway-capable units birth. A pure
  agent-runner gets its connection facts from `ava enroll` instead.
- `--cluster-secret TOKEN` — compatibility argv input for the cluster secret;
  prefer the URL-safe `AVA_INSTALL_CLUSTER_SECRET` one-shot environment input.
- `--worktree` — births a dev worktree's cluster at `--home` (default
  `~/.ava-<checkout-dir>`, derived from the checkout location, never cwd) and
  writes the checkout's `.ava_home` pointer. Refuses the default home and any
  already-claimed home.
- `--seed --seed-source ENV` — copies the `seed_allowlist()` allowlist from the
  stated source (LLM + web-search keys, enabled provider-plugin keys, plus the
  DashScope base URL a workspace key is minted against). The **cluster secret is
  never seeded**;
  `--seed-source` is required (no default).

## Notes

- Birth is idempotent: re-running the install against an existing home
  reconciles it rather than allocating a second slot.
- Because the registry is keyed by home path, two co-located clusters cannot
  collide on identity — they differ by directory, not by a name kept correct
  inside a shared instance.

## Key dependencies

- [[cli.ava.okf.md]] — the CLI overview: install births, `ava start` brings up
- [[../scripts/scripts.ava.okf.md]] — `install.sh`, which calls this as its final step
