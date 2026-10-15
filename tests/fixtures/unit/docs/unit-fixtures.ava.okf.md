---
type: doc
title: "Opt-in Unit Capabilities"
description: "Consumer-local homes, identity, generation ledgers, gateway clients, model owners, serving doubles, logging and retry observations."
tags:
- evaluation
- quality-assurance
---

# Opt-in Unit Capabilities

The root plugin roster does not load unit fixtures. Consumer-local conftests
import `unit.homes` (private and default homes) and `ava.tests.fixtures.workspace` (SDK workspace). The
fixtures of a component live in that component's `tests/`: `machine_identity` in
`base/cluster/machine/tests/`, `config_authority` and `served_gateway_home` in
`base/config/tests/`, and the private-ledger seeds (`seed_write_generation`,
`seed_active_generation`) in `base/host/db_authority/tests/`.
Descendant tests inherit those fixtures from their nearest shared test directory.
Directories already at their entry budget use the same existing scope reader
for a directory-level capability declaration instead of adding a conftest.
Indirect consumers count too: `config_authority` needs `unit_home`, and scoped
CLI and PTY fixtures keep their home dependency. `units` retains explicitly
imported agent allocation and environment-file helpers without loading the
assembled gateway.

Model consumers import the immutable catalog fixtures from `model_catalog`.
`base.config.tests.config_authority` owns the lightweight configuration authority and its
private-home dependency; `ava.tests.fixtures.model_owner` owns the explicit SDK installation
and opt-in process binding. A test that only needs authority therefore does
not import Installation or construct a model catalog. The fixture bodies and
their dependencies remain unchanged; consumers choose when to install the SDK
owner. Catalog mutation helpers keep their existing public type aliases.

Logging consumers import `log_capture.loguru_records`; retry consumers import
`retry_waits.retry_waits`, which records the retry library's bounded waits
instead of sleeping. It is never a global no-op wait: wall-clock loops must
still progress, and the existing wait-count bound remains enforced. Serving
lifecycle consumers import their package's `serving_root` observation double.
Those capabilities use local conftests at shared consumer test directories.
When a directory is full, its existing scope declaration names exact consumer
files, including child files when the full child has no declaration of its own.

`gateway.tests.agents.gateway_auth` (`default_gateway_headers`, `gateway_credentials`,
`without_credentials`) and `tests/fixtures/gateway_config.py` (`gateway_test_client`,
`gateway_test_app`, `gateway_test_process`) are where a Gateway test client's configuration and
credentials are assembled. The cluster API always authenticates, so `gateway_test_client` builds a
fresh `create_app(...)` instance and carries `Authorization: Bearer <cluster secret>`, the secret
from the resolved `GatewayConfig` when the client is built, unless the test passes its own
`Authorization` or `headers=None`. Unrelated tests never import the assembled gateway app. Tests
of rejection or admission use `without_credentials(client)` or `gateway_test_client(headers=None)`;
raw `httpx` clients take `gateway_credentials()`.

`gateway_unit` and `sdk_via_gateway` live in `tests/fixtures/unit/gateway.py`.
Their four consumer directories use file-level declarations in
`path_scopes.toml`; putting the assembled app in a shared conftest there would
make unrelated tests depend on every gateway router. These declarations use the
existing pytest/CI scope reader. They add no runtime binding mechanism.

`ava.tests.fixtures.sdk` owns the public fixture capabilities `sdk_environment`,
`sdk_identity` and `sdk_metering` beside actual SDK consumers. Its exact component
entry and static `__all__` name these definitions; the lightweight identity slot
protocol and its minimal context/client-close contracts are defined and exported
by `identity_restore`; the real SDK module satisfies the writable slot directly. The root identity and metering guards depend on ordinary fixture
overrides (`sdk_identity`, `sdk_metering`); their defaults carry no SDK state.
Each test assembles its SDK root from the prepared `process_config` and its
explicit `machine_identity`. The context, lazy DB factory and event pipeline
borrow that same machine owner. Repinning changes agent identity while borrowing
those clients, machine and clock. Teardown closes the owned ClientSet after the
identity and recorder guards finish, then restores the preceding context.
Recorder teardown restores `Installation.metered` before monkeypatch restores
the installation slot; full plugin teardown remains its separate owner. Direct
recorder callers restore their own returned ledger in `finally`.

SDK qualification includes readers and local autouse setup/teardown, not only
explicit test arguments. Bind all three SDK fixture exports at the nearest
consumer test owner. Mixed directories can use their existing literal scopes;
ordinary conftests also cover descendant collectors. A function provider does
not protect collection-time SDK state reads: collection declarations and
imports must remain independent of the bound context. Open dynamic imports
remain Unknown in source analysis; they are not a negative qualification rule.

Global environment boot prepares private-home pins without constructing Settings.
The session `process_config` fixture snapshots configuration when a consumer
requests it, before that consumer's bare function home. Native DB/Redis resources
follow their independent fixture dependencies; unrelated tests retain refusal.
Plugin restoration, leak checks and lightweight host guards remain global. A bare home remains bare; the
SDK environment never bootstraps configuration from that directory.

## SDK identity restoration

The identity a test acts as is bound bare by hundreds of tests (`pin_agent(...)`, `ava.tests.fixtures.pin_agent`: it binds an `AvaContext` carrying it), so reporting it would turn the convention into a defect. The root plugin `identity_restore` (`tests/fixtures/identity_restore.py`) receives the SDK consumer's explicit `ava.context` slot through the ordinary `sdk_identity` fixture override in `ava.tests.fixtures.sdk`. It closes only clients created by the test, then restores the previous SDK context even when close fails. Unrelated tests use the lightweight default without importing the SDK. Native admission scopes do not provide or rebind SDK identity. `tests/ci/test_leak_guard.py` checks that boundary, that `ava.sdk_surface.agent_identity` holds no process-global slot of its own, and that the restore isolates the next-test victim. `ava.self.AGENT_ID` is not touched: the module `__getattr__` serves it, and writing a read value back would store it for good (PR #3791); a test that stores it is a `module-attr` leak.
