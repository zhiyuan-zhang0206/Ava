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

## Running a non-default model (per-session override)

`spawn_codex.py` always launches the machine's default model — it takes no
model flag, and that is deliberate. **Never change the global
`~/.codex/config.toml` to switch a session's model.** Several agents share
that file; an edit-restore window races between them and has left stale
residue in the field. The cluster rule is per-session override only
(2026-09-23).

To start on a specific model, pass the override on the command line — the
same `-m` / `-c` flags work for the TUI and for `codex exec`:

```bash
codex -m <model> -c 'model_reasoning_effort="xhigh"' \
  -C <workspace-dir> --dangerously-bypass-approvals-and-sandbox
```

Verify the banner before trusting the session (`model: <model> <effort>` and
the directory), and decline any "upgrade codex" prompt — a session must not
self-upgrade.

A hand launch is **not canonical**: no generation is registered, no task or
work files are created, and no supervisor starts — the ownership and
supervision the spawn script provides are yours to reproduce. Run it under
the same file-driven discipline as Mode A (task file + work file +
`watch_work.py` to wake you), and keep to one live Codex per workspace so a
second launch cannot race the first.

When the default `~/.codex` home should stay untouched entirely, give the
session a private home: create the directory with mode `0700`, symlink `auth.json` in from
`~/.codex/`, copy `config.toml` and append the workspace trust row
(`[projects."<workspace-dir>"]` / `trust_level = "trusted"`), then run with
`CODEX_HOME=<dir>`. This isolates configuration and session state; auth
stays shared by design — a token refresh is written through the symlink, so
both homes see the same account (a frozen copy would go stale on rotation).

Before a large session, check the account's remaining quota at zero token
cost — the app-server `account/rateLimits/read` request, or `/status` where
the TUI surfaces it. This is a ChatGPT-account surface: a plain
`OPENAI_API_KEY` session has no plan quota to read. If the quota is nearly
exhausted and no reset is imminent, report that before launching instead of
parking a session that will die mid-task.
