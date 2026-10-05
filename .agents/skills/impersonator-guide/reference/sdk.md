SDK reference for the [impersonator guide](../SKILL.md). Read it before your
first attachment; use the main guide for receipt, renewal and release.

# Using the Python SDK under the lease

Ava's SDK is a Python namespace (`ava.*`). Under the lease you do not run the
Ava model — you attach your own Python process to the lease and call the SDK
directly with the borrowed identity.

User-visible replies never go through the attachment — send them with the CLI (`ava impersonate say <session_id> --agent <agent_id> --key <key> 'text'`; see the main guide's message-handling section).

Use direct Python as the normal SDK path; no CLI prefix or translation of
`ava.*` code is needed. Run a short script or heredoc with the cluster checkout's
`.venv/bin/python` from your executor session's own shell. Keep the inherited
`AVA_HOME` and use that checkout's interpreter, not a development worktree.
Each invocation may attach, do its work and close; no persistent Python process
is required. For example, read the standing instructions first:

```bash
/path/to/cluster-checkout/.venv/bin/python - <<'PY'
import ava

with ava.external.attach(0, agent_id=405) as attachment:
    print(attachment.instructions())
PY
```

Replace the example path and ids with your cluster checkout and active lease.
Read the returned text before acting. Subsequent Python operations use the same
attachment pattern:

```python
import ava

session_id = 0                 # from the activation push
agent_id = 405                 # the Ava agent you are replacing

with ava.external.attach(session_id, agent_id=agent_id):
    ava.agents.send_message(406, "Implementation ready for review")
```

Inside the attachment the SDK resolves identity, plugins, and configuration as
the borrowed agent; peer messages and spawns carry that identity. The context
manager stages plugin state and flushes it on exit; for a long session call
`attachment.flush()` between steps — it never renews the lease.

Messaging another Ava agent needs no attachment — the CLI carries the borrowed
identity, attested like every control command:

```bash
ava impersonate send <session_id> --agent <agent_id> --to <target_agent_id> --content 'Status: X done, Y open.'
```

The delivered source is `agent:<agent_id>`, exactly what `ava.agents.send_message`
stamps inside the attachment.

The optional CLI wrapper also runs a local Python file inside an attachment
without involving any Ava model:

```bash
ava impersonate exec <session_id> --agent <agent_id> --file operation.py
```

Omit `--file` to read the program from stdin. The file already runs inside
this session's attachment: call `ava.*` directly. Calling
`ava.external.attach()` again in it fails with "this process already has an
external attachment".

Boundaries: `ava.self.compact`, `ava.self.terminate`, and `ava.self.restart`
end the *native* agent's execution loop — they are not yours to call. If the
work concludes the agent should compact or reconfigure, say so in the release
summary. Durable lifecycle requests for the agent (`ava.agents.restart`,
`ava.agents.terminate`) reach the native dispatcher while it is parked, but
treat them as last resorts: flush pending plugin state first, and prefer
leaving lifecycle decisions to the Ava side.
