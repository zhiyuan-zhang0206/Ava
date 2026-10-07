---
type: doc
title: Weixin Durable Ingress
description: "Account-bound provider source retention, frozen business routing and opaque cursor cutover."
tags: []
---

# Weixin Durable Ingress

The gateway-side bridge's existing Weixin poll loop owns `ingress/`. A qualified
source is canonical HTTPS provider namespace, bot account, sender and exact
positive uint64 provider ID. Text/time hashes and `_seen` never establish
admission. Known `FINISH` messages with supported raw field types qualify;
missing/incomplete/unknown states, IDs or enums hold the cursor for inspection.
Known unsupported media and filtered bot/group/self events retain explicit
terminal decisions. Unknown slash commands use the native chat owner.

`weixin_ingress_receipts` retains the first full provider payload, immutable
semantic body (including validated raw item kinds/content) and original route.
Ordinary chat commits that receipt together
with `inbound_messages` and its central audit through the caller-owned native
chat writer. Later selection changes cannot redirect a replay. Receipt IDs and
inbound IDs are snapshots without foreign keys or TTL: deleting the inbound
never recreates accepted work. A changed same-source body conflicts. The existing
pending-inbound watchdog covers missed post-commit wakes; acceptance does not
prove execution or agent readiness.

Existing `weixin-chat-v1:` raw inbound receipts can be adopted atomically before
current selection is considered, preserving the original target and inbound ID.
Only an existing matching chat/user/body row proves that legacy acceptance.
Deleted legacy receipts and unkeyed history cannot be reconstructed from text,
provider time or ID ordering. New source tombstones outlive inbound retention.

## Honest command results

First routing freezes the original notice ID/agent ID or account-qualified
command context/draft before a source is claimed. The existing core parser and
command owners execute once under a retained attempt UUID. Only actual native
selection receipts, notice inbound IDs or acknowledged spawn birth IDs prove
business acceptance. Read/menu commands and notice callbacks whose existing
owners return only human hints remain `uncertain`; no Reply text is parsed as
proof. HTTP failure, missing/malformed owner proof or a process interruption
also remains `uncertain`, never automatic retry, fallback or a new key. The
normal legacy spawn endpoint is called once; this does not promise recovery of
its unknown result or an atomic multi-command conversation.

After restart, previously `claimed` commands become `uncertain` with the same
attempt and `command_attempt_unresolved`; this says the outcome is unknown,
not that its caller died. Retained, never-claimed sources recover their frozen
route through the same poll loop. Business proof commits before best-effort
human hints; hint failure never demotes acceptance. An unresolved attempt cannot
be re-executed by replaying the source.

Operators inspect receipt ID, account/source identity, status, attempt UUID,
`outcome_reason`, frozen route and original result in these tables. Payloads may
contain session tokens: inspect them only through authorized database access;
do not copy payloads into logs or tickets. There is no automatic command
reconciliation worker. Reconcile an uncertain source against the actual domain
owner's birth/notice/selection evidence; this PR provides no operator rerun or
mark-success command. Keep the source retained if proof is unavailable.

## Cursor and account cutover

`weixin_ingress_bindings` owns the current namespace/account and poll epoch;
`weixin_ingress_cursors` owns each account's opaque cursor and cutover state.
Every admission, claim and checkpoint locks and verifies that epoch. An old
in-flight poll cannot write or checkpoint after replacement. A batch checkpoints
only after all items are durably retained as accepted, explicit terminal or
uncertain; retained/claimed business attempts prevent advancing it. Source ACK
means the source is retained, not that its business action executed.

Both legacy JSON cursors and missing JSON start `held`: neither account binding
nor file absence proves no prior processing. Before enabling this code in a
running installation, an operator must stop/drain old producers, preserve the
legacy cursor/history/journal and authorize the exact account/cursor binding.
`WeixinIngressStore.begin_cutover(binding, expected_cursor=...)` is the explicit
native owner action, using the configured gateway-side pool and current epoch.
It moves `held` to quarantine-only `draining`, never directly to active.

The existing poll loop retains **every** unproven legacy backlog item across
multiple batches, executes no commands and emits no new chat during draining.
Matching retained raw chat receipts may be adopted. Only a real provider
response with an explicit empty `msgs` list and returned opaque cursor, committed
together, changes the account to `active`. Missing `msgs`, missing cursor or a
failed checkpoint is not empty-history proof. No timestamp/ID cutoff is guessed.
This operator cutover protocol is also required for an installation without
verified history; deployment/binding/draining is not performed by this PR.

Routing uses only the account-matching native selection. Unbound or other-account
reply mode holds the source as quarantined and preserves the mode, never resolves
its notice or falls back to chat. Provider context-token/activity caches use
namespace/account-qualified private files; old peer-only caches remain untouched
and are not imported. Account replacement does not authorize old routes. Native ingress retains its
original registered adapter: replacement rejects old calls before a new poll.
Selection writes freeze that adapter/account and verify the source epoch and
claimed attempt inside the same selection transaction. Binding locks serialize
account replacement with an already admitted original-account effect; neither
case borrows the new account. Human hints use the original adapter or are skipped.

Real Weixin requires account-proven selection. Registration of a replacement
cancels old derived subscriptions/typing and clears their runtime agent, without
deleting pending outbound intents, historical selections or legacy JSON. Native
selection restoration and background synchronization cannot import peer-only
state; the active poll loop reconstructs subscriptions only from matching
canonical selection. Post-commit subscription failure leaves source acceptance
intact and the same loop can recover it.

Legacy Weixin `outbox.jsonl` entries lack provider/account proof and stay held
with their original payload and key; no HTTP replay or minted identity occurs.
Other channels continue their existing journal drain. An operator must retain
and inspect those entries separately rather than treating new binding as proof.

[Official client types](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/types.ts)
provide uint64 IDs, message states and the opaque cursor shape. They do not
establish globally unique IDs, immutable same-ID content, indefinite replay
retention or exactly-once provider delivery. This source ledger makes its own
qualified acceptance and uncertainty explicit. #4470/#4477 remain open; no
deployment or client Outbox is included.
