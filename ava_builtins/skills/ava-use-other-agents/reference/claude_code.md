# Claude Code (`claude`)

For an interactive (persistent) session, use the spawn script instead of
launching by hand — it pre-trusts the directory, presets Claude Code's
first-run dialogs (bypass-permissions confirmation + fullscreen upsell;
persisted into `~/.claude/settings.json` / `~/.claude.json`, backed up and
idempotent) so an unattended spawn cannot park on one, and sends the
contract message. The session resolves `claude` from its PATH, then tries
`$HOME/.local/bin/claude`. If Claude's UI does not appear, the launcher stops
before sending the contract or takeover bootstrap:

```bash
.venv/bin/python reference/spawn_claude.py <workspace-dir>
```

For a takeover (`--impersonate-self --impersonation-name '…' --brief '<text>'`)
the script is file-less: no task/work files, nothing to watch, and the session
relay starts automatically via the bundled `ava-relay` plugin (resident mode;
`--no-relay-resident` restores the executor-armed flow). The full procedure is
[Let the coding agent take over your identity](impersonate_self.md).

Manual launch (for full control):

```bash
cd /path/to/workspace && unset ANTHROPIC_API_KEY && claude --dangerously-skip-permissions   # interactive session (the persistent pattern)
```

Headless one-shot (`claude -p "<task>"` / `claude -p "<task>" --output-format json`) is
available for rare, self-contained tasks that need no supervision — avoid by default.

- **Required flags on this machine:** `--dangerously-skip-permissions`.
  Available models: Fable 5 (`--model fable`), Opus 4.8 (`--model opus`).
  Other flags (verify with
  `claude --help`): `--output-format
  json|stream-json` (only with `-p`), `--continue` / `--resume <session_id>`.
- In an interactive session, `/compact` (and silent auto-compaction near the
  limit) manage context; `/context` shows usage but as a grid, not a number.
- `--output-format json` returns per-turn token usage (input / output / cache)
  and the model's `contextWindow`; it does **not** report cumulative session
  usage — sum it yourself if you track headroom in headless mode.
- **Auth & billing trap:** `ANTHROPIC_API_KEY` must be **unset** before launching
  `claude`. When the env var is present alongside a paid Claude subscription,
  Claude Code defaults to API-key billing — incurring per-token charges instead
  of using the subscription. The spawn script already unsets it in the session.
  For manual launch, prefix the command with `unset ANTHROPIC_API_KEY &&`.
  (Agents calling Claude through Ava's model layer are unaffected — the key is
  picked up from the server config, not the agent's environment.)
