# Delivered upload recovery

Approved design with an implementation candidate; repository merge and deployment are not implied.
Current contract: [delivered upload owner](../../../gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.ava.okf.md).
Issue #4476. Audited from fourth integration `ae2b2f903266ade04a7831f83ee06d44bd40a8fb`.

## Scope

Close source-file, remote-copy and single-notification crash windows for one
authenticated upload intent. Keep synchronous legacy uploads and silent-upload
contracts unchanged. No client Outbox, automatic client activation, generic
workflow engine, TTL abandonment or staging sweep. A 202 means durable source
acceptance, not remote readiness or agent execution.

## Prior art

- `gateway/routers/upload/batches.py`: retained receiving manifest/reservation,
  per-agent xact quota gate, create-only publication and fsync before ready receipt.
- `base/host/private_storage.py:create_private_bytes`: complete, fsynced,
  non-overwriting publication; a duplicate verifies the existing object.
- `base/agents/messages/chat_delivery.py:insert_chat_inbound_in_transaction`:
  native chat identity/provenance/audit writer without commit or wake. Its own
  inbound-row identity disappears on deletion; upload needs retained evidence.
- `gateway/app.py:lifespan`: owns background tasks and closes pools after task
  shutdown. `gateway/http/middleware/stopping.py` exposes actual server shutdown;
  `base/deploy/maintenance/admission.py` owns pause/quiesce decisions.
- `services/agent_runner/agent_ops/daemon.py`: authenticated known-kind admission,
  maintenance request/worker tracking; `dispatch_sync.py` owns blocking receivers.

The existing Ops outbox loop is suitable mechanically, but the canonical roster
(`ops/roster/__init__.py`) starts Ops only for agent-runner capability. A pure
Gateway has no Ops. Changing roster/credential profiles or restricting support
to a single box is rejected. Source recovery belongs in the existing Gateway
lifespan; the target receiver remains in Ops. Existing `upload_receive` overwrites
files and cannot authorize strong replay.

## Simplest sufficient design

### Acceptance and immutable files

New fixed `POST /api/keyed/v1/agents/{id}/uploads` requires verified principal-v1
and a valid caller key; it always promises one chat after confirmed copy.
Ordered raw display names, sizes, SHA-256, content types and delivery policy form
request identity. Replay checks that identity before mutable target facts.

Tx A reuses source quota admission and retained receiving identity. Fresh
admission freezes source unit, original target machine/unit, request provenance,
manifest and delivery policy. Freeze audit provenance, not bearer secrets; background
transport uses current native credentials. Preserve the upload contract's user
source rather than relabeling delivery as the worker. `agents_meta.machine` supplies placement;
`machine_units(machine_name, home)` supplies the existing unit identity. Resolve
exactly one serving runner unit matching the registered Ops URL; ambiguity or
unknown placement rejects before claim. Receiver checks its actual machine/home,
so changing a registry URL cannot redirect this intent into a different home.
No new machine registry or hardware identity is proposed.

Tx B verifies/fills the fixed objects with the existing private-storage owner,
fsyncs final directories, and commits source-ready receipt plus delivery intent
in the **same transaction**. Only then return the immutable 202 acceptance and
status reference. A committed receiving row without source-ready can resume only
with the original key/bytes; identity and reservation never expire.

Use a hidden `.delivered-v1/<batch UUID>/` physical namespace for this new mode,
with UUID/ordinal/true-suffix leaf names and original display names in manifest.
This is necessary on the receiver: even an old `upload_receive` replaces a flat
sanitized filename. Its separator sanitization cannot address this nested
namespace, so a disconnected legacy writer cannot overwrite guarded finals.
Keep silent flat names unchanged. A new authenticated, manifest-bound object GET
serves only source-ready objects by batch/ordinal (not arbitrary caller paths).
Source/receiver quota inventory must include ready guarded objects and count
receiving reservations once, excluding their present finals; never sweep staging.
Legacy disconnected unreserved writes remain an explicit quota limitation.

### Source worker and naturally repeatable receiver

A dedicated Gateway lifespan round selects due intents with short native row
locks, then releases the connection **before network calls**. Retry scheduling
is backoff, not a lease or proof another writer died. Multiple HTTP workers may
copy the same immutable batch concurrently; receiver publication is repeatable
and final inbound acceptance is serialized. No persistent exclusive worker claim
or time-based takeover is needed. Reuse the existing data-plane pool (max 8),
not the reserved control pool, with one connection per short transaction.

New `upload-receive-v1` is an independently known OpKind, not a payload added to
old `upload_receive`. Freeze target unit and immutable object references; validate
strict sizes, SHA-256, ordinal/name safety and target identity before effects.
Unknown old kinds fail before dispatch/dedupe; never fall back. Keep this naturally
repeatable kind outside transport uncertain-claim caching and send no transport
idempotency key; domain batch identity is the recovery owner. Each receiver
has a retained batch/target-unit manifest and receiving quota reservation. The
file phase runs without a borrowed DB connection, publishes only create-only
files, verifies existing bytes, and fsyncs. Its ready transaction records copy
acceptance. Repeated requests verify/fill actual files before issuing the typed
matching-manifest proof; cached RPC success alone is not present-file proof.
A local shortcut requires the exact same source/target unit, not just same name.

The source requires an exact supported proof version and validates the new
receiver result against frozen unit/hash/size and
records remote proof. Timeout, response loss or native restart permits repeating
only this same copy. Unsupported protocol, checksum conflict, absent/ambiguous
unit or changed placement becomes bounded inspectable HOLD with a safe reason;
no redirect, overwrite, fallback or fabricated ready outcome. A retryable outage
keeps pending state/backoff. A future reviewed operator policy may cancel an undelivered intent or restore its frozen target; no cancel/unseal endpoint is implemented. Retargeting is a new intent, never receipt mutation.

### One retained inbound acceptance

After proof, a short write transaction locks the delivery intent, rechecks the
original target placement and agent existence, invokes the native public chat
writer with batch-derived client-message ID and frozen provenance, and stores
original inbound ID plus immutable delivery outcome in that same transaction.
A retained accepted outcome is consulted first on recovery, even when the inbound
or agent has since been deleted; it never recreates a deleted IID. New deleted
or moved targets HOLD without notification. Target termination/resurrection uses
the existing pending-inbound policy after commit; acceptance is not execution.
Post-commit wake is a hint, repaired only for the original still-pending row.

The implementation status distinguishes receiving, pending-copy, inbound-accepted
and HOLD. Copy proof commits with the final inbound outcome rather than exposing
a separate durable copy-verified phase. Source acceptance and final delivery snapshots
are immutable; status is an authenticated operator projection, not a rewritten
202 receipt. No FK/TTL deletion of identity, reservations or accepted evidence.

### Lifecycle and boundaries

The lifespan stops admitting rounds on stopping/business pause, drains actual
owned async HTTP and shielded DB/filesystem futures, then closes clients/pool.
Do not cancel `to_thread` and pretend its native writer ended. Hard process death
leaves fixed files/retained intent for recovery, never authorizes cleanup.
Remote Ops keeps its existing maintenance admission and worker-future tracking.
A slow copy must not starve unrelated intents or health: bounded batch/network
attempts and bounded rotating scans, not a new worker framework.

Shared manifest/storage/receipt facts belong below Gateway/Ops in `base.agents`
with a focused upload package; Gateway owns acceptance/source round, Ops owns
receiver and its wire models. `base` must not import Gateway/services, Ops must
not import Gateway, and no service may import CLI/kernel. Any extraction must
trace consumers/module moves and preserve silent-upload namespace/receipts.

## Required fault proof before publishing a path

| Fault / concurrency | Required result |
| --- | --- |
| Tx A commit / before source fsync / after fsync before Tx B | same fixed receiving identity; quota once; fill/verify finals |
| ready commit response lost / changed manifest | original 202 replay / 409; no fresh copy intent |
| receiver after fsync timeout / parallel copies / native restart | same bytes and manifest; no overwrite or uncertain dedupe claim |
| receiver ready DB loss / disconnected old writer | retained reservation and safe replay; nested finals unchanged |
| source network cancellation / shutdown / multiple Gateway workers | no borrowed DB socket across RPC; drain actual futures; one inbound |
| inbound before commit failure / commit response loss | atomic rollback or original retained IID/outcome |
| accepted IID deleted / agent deleted / placement changes | no requeue; historical replay or fresh HOLD, never new home |
| checksum/name/quota conflict / old unknown kind / maintenance hold | zero unauthorized effect, truthful status, no legacy fallback |

Run real PG17, hard-process and filesystem fault tests, actual Gateway lifespan
and Ops dispatch/auth/maintenance consumers, plus silent-upload URL/MIME/native
image regressions, schema convergence and normal hooks. The new delivered object
URL is not silently enabled for existing multimodal silent-upload consumers.

## Review choices

Approve the Gateway-owned round, hidden delivered namespace/object route and
unit binding before code. This cannot guarantee access after operator file deletion,
replace legacy late-write quota reservations, recover missing receiving bytes
without a caller, or infer agent execution from notification acceptance. Complete
the remote receiver and inbound transaction chain before exposing guarded 202.
