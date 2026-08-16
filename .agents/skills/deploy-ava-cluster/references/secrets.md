### Secrets reference

`.env.example` is grouped the same way. Fill the model provider key(s) your
`AVA_MODEL` needs, plus the capability keys for the features you want — a missing
capability key does not block start, it just makes that one feature raise when an
agent reaches for it.

| Key | Enables | Needed when |
|---|---|---|
| `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | the chat model | `AVA_MODEL` (or a per-agent override) uses that provider — `deepseek-*` / `claude-*` / `gpt-*` |
| `GLM_API_KEY` / `MIMO_API_KEY` / `MOONSHOT_API_KEY` / `XAI_API_KEY` | the chat model | `AVA_MODEL` (or a per-agent override) uses that provider — `glm-*` / `mimo-*` / `kimi-*` / `grok-*` |
| `GEMINI_API_KEY` | `ava.understand` media path (image/video/audio/PDF) | always, in practice — the default media model is `gemini-3.5-flash` |
| `BRAVE_API_KEY` | `ava.web.search` | you want web search |
| `JINA_API_KEY` | `ava.web.fetch` (higher rate limit) | optional — empty = anonymous tier |
| `AVA_TELEGRAM_BOT_TOKEN` / `AVA_TELEGRAM_OWNER_ID` | push to your phone (the `telegram-send-file` skill) | optional |
| `AVA_TRACE_ENABLED` | record OTel spans to the local `$AVA_HOME/traces/` mirror | optional — on by default (network-free) |

### `AVA_CLUSTER_SECRET` — the cluster control credential

One URL-safe token per cluster, minted at install and never rotated by a
re-install. The gateway API and each runner's `/ops` accept it as a bearer
token. It is also the gateway/main Postgres identity's password and the Redis
ACL password (each cluster's Redis is single-tenant, so `requirepass` is the
same secret — there is no separate box-level admin secret).

Remote runners are least-privilege at Postgres: the gateway provisions an
independent `ava_runner` role with `AVA_RUNNER_DB_PASSWORD` and projects that
credential inside the runner's bootstrap `AVA_DB_URL`. The standalone runner
password is never served as an env key, and `shared/config/data_plane.py`
deliberately does not overwrite it with `AVA_CLUSTER_SECRET`. Runner Redis and
HTTP `/ops` access still use the cluster secret.

Which install shape gets one is decided by `--role`:

| Install | Secret |
|---|---|
| `--role gateway,agent-runner` (single box) | **empty** — a NO-AUTH cluster serving unauthenticated on loopback. Read a token without echo, export it as the one-shot `AVA_INSTALL_CLUSTER_SECRET`, run install, then unset it to turn auth on. |
| `--role gateway` (split gateway) | **minted automatically** — remote runners depend on it. Transfer it through the operator's secret channel, then expose it to `ava enroll` as `AVA_CLUSTER_SECRET` (not an argv value). |
| `--worktree` (dev cluster) | **empty**, and never inherited from prod. |

To opt a single-box install into auth without putting the token in shell history
or process argv:

```bash
printf 'Install cluster secret: ' >&2
IFS= read -rs AVA_INSTALL_CLUSTER_SECRET
printf '\n' >&2
export AVA_INSTALL_CLUSTER_SECRET
./scripts/install.sh --role gateway,agent-runner
unset AVA_INSTALL_CLUSTER_SECRET
```

A secret already present in the `.env` is never overwritten, so a re-install is
safe; rotate deliberately with `scripts/rotate_cluster_secret.py`. Gateway/main
Postgres URLs and all Redis URLs re-derive their live password from
`AVA_CLUSTER_SECRET` on load, so a stale embedded password cannot reach the
wire. The `ava_runner` Postgres URL is the explicit exception: bootstrap
projects its separate password, and rotating that credential is independent of
rotating the cluster secret.
