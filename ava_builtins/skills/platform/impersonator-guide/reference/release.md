# Releasing an impersonation lease

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Finishing: release with a summary

When the work is done — or when you must stop before it is — close attachments,
acknowledge any batch you received but have not yet acknowledged, and release:

```bash
ava impersonate release <session_id> --agent <agent_id> \
  --summary 'Implemented X and verified Y. Z remains open; resume from its failing case.'
```

The summary is required, nonempty, and concrete: state what you did, what you
verified, what remains open — including work whose input you acknowledged but
did not finish; an ACK means received, never done — and where to resume. Ava writes one JSON file at
`<agent workspace>/impersonation/<session_id>.json`, containing every incoming
and outgoing message, ACK state, lifecycle history, consumed SDK/API events and
statistics. Your summary plus that file path is the first new system note in
the resumed agent's input. History is permanent. The resumed agent must review the file's incoming
messages — including the ones you acknowledged, since an ACK records receipt,
not completion — and finish what remains. Expiry has no invented summary. Release, not silence, is the ending: never leave an active lease
behind when you are finished.
