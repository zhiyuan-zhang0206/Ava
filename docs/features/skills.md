# Skills

Ava reads the open **Agent Skills** standard (SKILL.md) — the format Claude
Code popularized and Codex, Cursor, and Gemini CLI now share. Any standard
skill folder installs unmodified.

## Why it matters

- **Zero rewrite** — install from a git URL or straight off disk; no manifest
  to author, no conversion step.
- **Index vs body** — a hundred installed skills cost a hundred description
  lines in the prompt, not a hundred bodies; full text loads on demand
  (`ava.help(ava.skills.<name>)`).
- **Ecosystem-compatible** — skills written for other SKILL.md tools work as-is.

## How it works

```bash
ava skill install https://github.com/some-org/pdf-skills
ava skill install ~/my-project/.claude/skills
```

Installed skills mount as a namespace mirroring the folder shape; the
`# Capabilities` index in the system prompt lists name + one-line description,
and a drift check names newly installed skills before the next LLM call.

## External operator context

Production host convergence exposes one repo skill to already-installed Codex
and Claude Code clients: `operating-ava-cluster`. If `~/.codex` or `~/.claude`
exists, Ava copies that package to the client's global `skills/` directory. It
does not create absent client homes and does not distribute the rest of
`.agents/skills/`.

The copy's marker must match a private per-client ownership ledger under the
prod cluster home. Its digest includes names, kinds, bytes, and modes. A
per-target process lock serializes claim-and-verify updates and crash recovery.
The ledger keeps an expected manifest for every transaction-owned generation
and a terminal record for privately retained cleanup residue.
It writes a durable phase before stage publication and prior-target claim, then
reconciles the atomic no-replace result from source/destination presence plus
the transaction marker, digest, and manifest. Ambiguous paths remain preserved
and fail-closed. Cleanup write-ahead claims each residue tree into the private
ledger root, then durably records each file's source, claiming, and quarantine
states around a second no-replace rename. It never infers file ownership from a
deterministic name or matching content after an ambiguous restart. Since no
portable identity-bound unlink is available, the isolated copy is terminally
retained without pathname unlink, chmod, or retry; the client target remains
unblocked. Multi-link files are rejected.
An unmanaged target, modified managed copy, unsafe linked path, or cleanup
residue that no longer matches its ledger is preserved and reported as a
client-labelled conflict. Client integration failures are warning-only for core
converge; repository source-integrity failures remain fatal.

## Design decisions

- [Skill system](../../okf/skills/skills.ava.okf.md)
- [External-agent operator bridge](../../okf/skills/external-agent-operator-bridge.ava.okf.md)
- [Skills and MCP](../../decisions/2026-05-05-skills-and-mcp.md)
- [Skills single load dir](../../decisions/2026-07-14-skills-single-load-dir.md)
