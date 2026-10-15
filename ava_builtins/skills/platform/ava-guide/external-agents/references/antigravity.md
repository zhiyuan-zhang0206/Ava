# Google Antigravity (`agy`)

Launch a persistent Antigravity session in a workspace with `spawn_antigravity.py`:

```bash
.venv/bin/python scripts/spawn_antigravity.py <workspace-dir> --prompt '<initial message>'
```

Without `--prompt`, `spawn_antigravity.py` creates `tasks.md` and `work.md` (unless already present), opens an `ava.shell.sessions` PTY running `agy chat -a <workspace>`, and sends the file-driven collaboration contract. With `--prompt`, it launches `agy chat -a <workspace>` and sends your inline message verbatim (no task/work files are created).

On success it prints one `key=value` per line:

```text
session_id=<id>
antigravity_session=<conversation-uuid>
tasks_file=<absolute path>   # only in Mode A (without --prompt)
work_file=<absolute path>    # only in Mode A (without --prompt)
```

- `antigravity_session` is Antigravity's own conversation UUID (`ANTIGRAVITY_CONVERSATION_ID`).
- Pass `--resume <antigravity_session>` to reopen that conversation with `agy chat -r <uuid> -a <workspace>`.
- For a takeover (Mode B), pass `--prompt` instructing the spawned Antigravity session to read `impersonator-guide` and run `ava impersonate request --agent <agent_id> --provider antigravity --ttl 1800 --batch-window 0 ...` (`--thread-id` defaults to `ANTIGRAVITY_CONVERSATION_ID` when set in the Antigravity environment).
