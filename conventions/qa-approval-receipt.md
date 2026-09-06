# Exact-head QA receipts

The `qa-approved` label is necessary but insufficient. The gate fetches current
GitHub records and requires current-head approval by the configured authorized
GitHub account, or a genuine current-head approved review. The shared account
authenticates account authorization, not independent Ava reviewer identity.

An authorized reviewer posts a comment consisting only of this fenced JSON,
substituting the actual PR number, full current SHA and asserted reviewer ID:

```ava-qa
{"ava_qa_version":1,"pr_number":42,"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","verdict":"approved","asserted_ava_reviewer":"405"}
```

`revoked` withdraws approval. Comments for another SHA never carry forward.
The fence may be `json` or `ava-qa`. Optional `time` and `note` strings are
accepted for the existing reviewer workflow; they never influence trust or
ordering (GitHub record timestamps are authoritative). Unknown keys fail closed.
Edits/deletions and label changes re-evaluate records. Current-head dismissed or
changes-requested GitHub reviews veto receipts until a new actual review; their
`submitted_at` is not a dismissal timestamp and must not be used to hide a veto.
Do not manufacture a receipt on another reviewer's behalf or infer a SHA.

The evaluator runs trusted default-branch code and writes a commit status named
`qa-approved-gate` on the exact target SHA. Review events only emit a read-only
signal; a default-branch workflow then fetches authoritative evidence. No PR
checkout executes with the evaluator's write token. Queue exemption requires
the verified Trunk bot account ID, same repository, main base, draft status and
the actual synthetic ref shape: `trunk-merge/pr-<number>/<uuid>`, optionally
ending in exactly one `-bisection` suffix when Trunk splits a failed batch.
Both forms require every identity and repository check; branch names alone
confer no authority.

Activation requires reviewer workflow adoption and verification that required
status checks consume the exact-head status. Existing legacy check runs with
the same context can require a fresh PR head during migration. This change does
not change queue method, enqueue PRs, or independently authenticate Ava agents.

## Ready notifications

The PR's current-head receipt and evaluated gate are the shared QA record;
chat messages point to that record and do not replace its verification.
Unless the brief names another single reporter, the reviewer reports readiness
directly to the agent responsible for enqueueing after checking the current
head, receipt, gate, and required CI. Include the PR, full head SHA, and receipt
link. Do not also ask the author to relay the same ready result to that agent.

Authors send findings and fixes directly to the reviewer. Once the action
owner has the ready report, authors do not forward it again or copy it into
that owner's task log merely to record the relay. A new head, blocker,
revocation, or changed result still needs reporting promptly; a known failed
delivery still needs retrying. This reporting contract grants no enqueue or
merge permission.
