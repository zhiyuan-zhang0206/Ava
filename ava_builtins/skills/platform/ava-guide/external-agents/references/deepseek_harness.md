# DeepSeek Harness (`dsh`) Reference

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) is a
Node agent harness built from Cordis plugins. Here it runs only as a **takeover**
(Mode B): `spawn_dsh.py` has no delegated-worker mode. Confirm flags with
`dsh --help` and `dsh --profile web --help` on the actual machine — the project
is a developer preview and its interfaces move.

## Install and credential

```bash
npm install -g @deepseek-ai/dsh     # the `latest` tag; `next` carries release candidates
dsh --version
```

dsh resolves its own model key, first match wins: the launching environment
(`DEEPSEEK_API_KEY=… dsh`), `$DSH_HOME/.credentials.yaml` (`refs:
DEEPSEEK_API_KEY: …`; `$DSH_HOME` defaults to `~/.dsh`), the working
directory's `.env`, then `$DSH_HOME/.env`. Keep the key in the credential file
(mode 0600) rather than in an agent's environment. Check it with one tiny run:

```bash
dsh --profile headless "Reply with exactly: OK"
```

## Take over yourself

Run from your own execution context, briefing inline:

```bash
.venv/bin/python scripts/spawn_dsh.py <workspace-dir> --impersonate-self \
  --impersonation-name 'Fix login' --brief '<the full briefing text>'
```

The launcher boots dsh's shipped `headless` profile in a persistent shell
session with the `ava-relay-dsh/ava-relay.mjs` plugin patched in. The plugin
opens one dsh session, submits the launch message, and starts the relay once
the executor's request writes its credential stub. The session runs with
`DSH_PERMISSION_MODE=danger-full-access` because nobody answers approval
prompts in a PTY. Inspect or stop the generation like the other launchers:
`--status`, or `--cancel-generation <generation>`.

The executor's own manual is the `impersonator-guide` skill and its
`.agents/skills/impersonator-guide/reference/deepseek_harness.md` in the Ava source checkout.

`spawn_dsh.py` has no `--resume`: its relay plugin opens a new dsh session on
every launch. After an interruption, launch a fresh takeover whose brief is
built from the handoff JSON ([Resume after an
interruption](resume_after_interruption.md)).

## Watching a takeover

dsh records the session under `$DSH_HOME/sessions/`, grouped by workspace.
A `dsh web` UI on the same `$DSH_HOME` lists it, and its Trajectory tab shows
every tool call the model made. The Ava side shows the same takeover on the
agent's normal timeline.

Relay mechanics and the operator-side setup are in
`docs/conventions/agents/agent-impersonation-hosts.md#deepseek-harness-dsh` in the Ava source checkout.
