# External-agent operator skill bridge

On 2026-09-04, Ava added a prod host-global converge step that projected the
repository's `operating-ava-cluster` skill into already-present Codex and Claude
Code global skill roots. The change addressed external agents that needed Ava
workspace and memory context while operating outside the repository checkout.

The implementation deliberately did not restore `.agents/skills/` as an Ava
fleet runtime source. It considered only `~/.codex` and `~/.claude`, skipped
missing homes, and copied only the operator skill. Copied targets carried an
Ava marker plus the digest of the last written content. That made unchanged
passes idempotent and updates safe while forcing both unmanaged collisions and
user edits into visible, non-destructive conflicts.

Updates used a complete sibling staging directory. An existing verified copy
was moved aside, the stage was activated, and an activation failure restored
the prior copy. Cleanup remained limited to the exact bridge target's unique
stage and prior-copy paths. Ordinary copied directories kept the contract
portable to Windows without symlink privileges.

The operator skill also gained a compact cross-agent lookup sequence: map an
agent through `ava agents ls`, resolve a same-host workspace from the owning
cluster home, never infer a remote workspace from the gateway home, and read
`memory/MEMORY.md` plus its linked entries before searching shared memory.

Decision:
[`decisions/2026-09-04-project-one-operator-skill-to-external-agents.md`](../../../decisions/2026-09-04-project-one-operator-skill-to-external-agents.md).
