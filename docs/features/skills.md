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

## Design decisions

- [Skill system](../../okf/skills/skills.ava.okf.md)
- [Skills and MCP](../../decisions/2026-05-05-skills-and-mcp.md)
- [Skills single load dir](../../decisions/2026-07-14-skills-single-load-dir.md)
