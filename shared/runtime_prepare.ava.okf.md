---
type: doc
title: Inactive runtime preparation
description: Offline private Python generation preparation and declared native capability proof, without activation.
tags:
- shared
- runtime
---

# Inactive runtime preparation

`runtime_prepare.py` prepares a Linux/macOS generation at its final, private,
inactive path. No production caller uses it. It does not activate, migrate,
stop services, download packages, update source, or fall back to editable code.

Inputs are a managed CPython tree, complete wheelhouse, hashed lock export,
application wheel name and schema digest. Their inventory determines the input
identity; installed bytes have a separate manifest digest. A new generation
retains a private interpreter/stdlib copy and a stdlib-created venv, installs
only local wheels with hashes/copy semantics, proves real service imports with
networking blocked, verifies schema/resource placement and seals an inventory.
Python launches use isolated mode and disable bytecode writes. Failure preserves
the serving pointer and retains the failed generation for operator inspection;
it is never silently reused or deleted.

Preparation commands run through the stdlib-only
[[process-group-closure.ava.okf.md|group-closure core]]; captured source
acquisition and application building use `posix_command.run_owned_command`,
whose exec domain closes through the same core. Each owns a launch-time process
group, retains its leader unreaped until confirmed group closure, and waits
for ordinary children to finish naturally within the command deadline.
Timeout/interruption closes that group before propagating failure; a successful
parent exit alone cannot certify unfinished child work. This does not contain
trusted build code that deliberately creates an independent session or group.

For a dynamically linked managed Mac Python, a copied venv executable resolves
`libpython` beside that venv. Preparation retains the declared dylib in the
venv's own `lib/` and verifies it against the trusted interpreter inventory
before invoking the venv. The final loaded-image proof still checks actual dyld
resolution; no loader environment override or external library path is added.

Kernel and OS system libraries are explicit trusted platform prerequisites.
Loaded application dependencies must resolve inside the generation; Homebrew or
other mutable dependency paths are rejected. The receipt declares loaded-image
paths without claiming to attest the host OS. Optional plugin/dlopen behavior,
Windows preparation, process privilege
separation and plugin state compatibility are not covered. Optional prebuilt
frontend and pinned collector inputs are copied privately and included in the
same generation inventory. Collector configuration and file queues stay in the
unit home; online preparation reuses the existing version/hash-pinned downloader.
The loaded wheel's collector path cannot fall back to that mutable home binary.
CI validates the retained binary's config and sends an actual loopback OTLP span
to its local file exporter with the checkout absent. This is not proof of
Tailnet/firewall approval or every enabled production collector component.
Same-UID chmod is accidental-write protection, not an adversarial security seal.

The declared import roots are CLI, exec child, ops spec, agent-host and gateway.
After imports, the proof performs local NumPy dot, Faiss add/search, Arrow IPC
roundtrip, and libpq version calls to exercise delayed native loads, with network
connections denied. Linux process maps or macOS dyld supply actual loaded images;
there is no second per-file ldd/otool emulation. All retained stdlib bytes are
hashed, but this is not a claim that every GUI/optional module works. A separate
receipt compares `_tkinter` availability in the managed input, retained base,
and venv; changing the retained base capability fails preparation. This avoids
misreading standalone `ldd(_tkinter)` as CPython's executable-RPATH behavior.

CI builds real wheel inputs and exercises offline preparation on Linux and
macOS arm64, retires the input interpreter/wheel paths, runs isolated imports,
rechecks the installed inventory and injects a failed preparation. The serving
pointer must remain byte-for-byte unchanged throughout. No cluster is booted.

The existing updater is deliberately not wired yet: supported-host proof, disk
budget/GC policy, recovery-point verification, migration compatibility, retained
LKG, consumer cutover and an old-orchestrator/legacy-writer barrier must precede
any runtime activation. This is a preparation primitive, not a second controller.

macOS activation additionally requires pre-stop read-only firewall audit of the
new interpreter path and actual Tailnet inbound verification. Existing
`ava firewall status` / `audit_allowlist` can supply evidence; automatic
`firewall sync` prunes/adds rules and is not authorized by a read-only preflight.
CI imports/loopback do not prove OS approval or off-box reachability. Missing or
unreadable approval must hold activation while the old generation still serves;
manual OS approval, when needed, belongs to the user. Never disable firewall/TCC.
