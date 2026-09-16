# OpenAI Codex (`codex`)

For an interactive (persistent) session, use the spawn script instead of
launching by hand — it pre-trusts the directory and sends the contract message:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir>
```

Manual launch (for full control):

```bash
codex --dangerously-bypass-approvals-and-sandbox -C <dir>   # interactive, hands-off
```

Headless one-shot (`codex exec "<task>" ...`) is available for rare, self-contained tasks
that need no supervision — avoid by default. `codex exec review --uncommitted` is the exception
for code review; `codex exec resume --last "<follow-up>"` continues a previous session.

- `codex exec` **always returns exit code 0**, even when the task failed — judge
  success from the output, not the return code.
- Useful flags (verify with `codex exec --help`): `-s/--sandbox`
  (`read-only` / `workspace-write` / `danger-full-access`), `-a/--ask-for-approval`
  (`on-request` / `never`), `--ephemeral`, `-C/--cd <dir>`, `--skip-git-repo-check`,
  `--json` (emit JSONL).
- For a **hands-off interactive** session use
  `--dangerously-bypass-approvals-and-sandbox` — the file-driven pattern can't
  answer approval prompts. `-s danger-full-access` alone is not enough: the
  default approval policy is `on-request`, so it still pauses to ask. Pair it with
  `-a never`, or use the single bypass flag. Intended for an already-sandboxed host.
- `codex exec --json` puts per-turn token usage on `turn.completed` events
  (`input_tokens` / `cached_input_tokens` / `output_tokens` /
  `reasoning_output_tokens`); it does **not** include a context-window percentage.
- In an interactive session, `/status` reports `Context window: NN% left
  (X used / Y)` and `/compact` (manual + automatic) manages context; the footer's
  context field is off by default in some builds.
- Codex expects a git repo; pass `--skip-git-repo-check` if it is not one.
- Auth: `OPENAI_API_KEY`, or `CODEX_ACCESS_TOKEN` for a ChatGPT-account login.
