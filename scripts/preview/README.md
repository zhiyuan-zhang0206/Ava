# Local branch preview

`local.py` resolves a local branch, fetched remote ref, or commit to one immutable
commit before preparing a disposable cluster. It can exercise an unmerged branch
without waiting for CI or a production gateway. Its result is evidence for that
revision and profile; it does not replace CI or authorize production promotion.

```bash
python3 scripts/preview/local.py run --ref codex/my-change
python3 scripts/preview/local.py run --ref origin/main --keep
python3 scripts/preview/local.py check /absolute/path/to/run
python3 scripts/preview/local.py stop /absolute/path/to/run
```

The controller records the commit and requested ref under `~/.ava-previews/<run>`.
It prepares a detached worktree, the candidate's own Python environment and
frontend dependencies. It then invokes the candidate's normal `ava start` with
`--worktree`, a private configuration file, and a persisted allowlist of gateway,
frontend, ops and agent-host. Initialization, retry and stop belong to the normal
lifecycle; the controller has no alternate service launcher.

Each run owns its home, registry, port reservation and native Postgres, Redis and
PgBouncer. It inherits only a small OS environment allowlist, with no production
URLs, bearer, provider keys, Python redirection or telemetry export. The model is
scripted, while agent creation, graph execution and `print(1 + 2)` run through the
actual gateway and agent-host. Success requires the recorded execution body to be
`3`, not merely a model response or a timestamp containing that digit.

On macOS the normal chain is `launchd -> signed helper -> ava-root`. Each run
uses its own helper artifact directory and home-specific job, preserving the
stable signing identity without replacing another home's artifact. Signing and
permission prerequisites must already be available. Linux runs ava-root without
a helper. This controller uses POSIX process and lock APIs.

A run normally stops in `finally`; `--keep` retains only a successful preview.
Teardown calls normal stop and destroy, then independently checks for surviving
processes, listeners and registry reservations. A failed cleanup stays failed in
`run.json`; the observer never deletes evidence to manufacture a clean result.
Logs and data remain for inspection. Concurrent lifecycle actions on one run are
rejected by the operation lock.

This profile proves source startup and real agent execution. It does not prove
sealed release update/rollback, multi-machine coordination, real provider behavior,
browser/computer permissions, or production cutover. Those require their own
maintained scenarios using the same lifecycle and transition APIs.

The `validate.sh` and `spawn-samples.sh` scripts operate an explicitly selected
already-running preview home. They resolve that checkout's gateway and credentials;
they are not cluster initialization or release-promotion entrypoints.
