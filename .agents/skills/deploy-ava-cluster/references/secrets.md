### Secrets reference

`.env.example` is grouped the same way. Fill the model provider key(s) your
`AVA_MODEL` needs, plus the capability keys for the features you want — a missing
capability key does not block start, it just makes that one feature raise when an
agent reaches for it.

| Key | Enables | Needed when |
|---|---|---|
| `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | the chat model | `AVA_MODEL` (or a per-agent override) uses that provider — `deepseek-*` / `claude-*` / `gpt-*` |
| `GLM_API_KEY` / `MIMO_API_KEY` / `MOONSHOT_API_KEY` / `XAI_API_KEY` / `DASHSCOPE_API_KEY` | the chat model | `AVA_MODEL` (or a per-agent override) uses that provider — `glm-*` / `mimo-*` / `kimi-*` / `grok-*` / `qwen*` |
| `GEMINI_API_KEY` | `ava.understand` media path (image/video/audio/PDF) | always, in practice — the default media model is `gemini-3.5-flash` |
| `BRAVE_API_KEY` | `ava.web.search` | you want web search |
| `JINA_API_KEY` | `ava.web.fetch` (higher rate limit) | optional — empty = anonymous tier |
| `AVA_TELEGRAM_BOT_TOKEN` / `AVA_TELEGRAM_OWNER_ID` | push to your phone (the `telegram-send-file` skill) | optional |
| `AVA_TRACE_ENABLED` | record OTel spans to the local `$AVA_HOME/traces/` mirror | optional — on by default (network-free) |

### `AVA_CLUSTER_SECRET` — the cluster control credential

One URL-safe token per cluster, minted at install and never rotated by a
re-install. The gateway API and each runner's `/ops` accept it as a bearer
token. It does not authenticate Postgres, PgBouncer, or Redis: the internal
data plane always authenticates with its own credentials, whatever the bearer.

Postgres application logins are write generations recorded in the gateway's
private `$AVA_HOME/db-authority/`: one gateway and one runner login inheriting
the `NOLOGIN` groups `ava_gateway` / `ava_runner`. The schema owner is `NOLOGIN`
and the gateway `.env` holds only the credential-free endpoint. Remote runners
receive the runner login inside their bootstrap `AVA_DB_URL`, never as a
standalone key. Runner Redis has the independent `AVA_REDIS_PASSWORD`, embedded
only in the bootstrap URL; Redis `default`/`requirepass` uses
`AVA_REDIS_ADMIN_PASSWORD`, file-only on the gateway. A remote-managed plane
keeps its provider URLs and `AVA_RUNNER_DB_PASSWORD`.

The first-start capabilities determine the initial control-plane secret:

| First-start shape | Secret |
|---|---|
| `--serve-gateway --serve-agent-runner` | Empty bearer by default; unauthenticated loopback API. Postgres/PgBouncer still admit only write-generation logins (generation 0 minted at first start) and Redis gets its generated admin and runtime passwords. |
| `--serve-gateway --no-serve-agent-runner` | Minted automatically. Transfer only the bearer to runners through the operator's secret channel. |
| `--worktree` | Single-box defaults; no production secret or data is copied. |
| `--serve-agent-runner --no-serve-gateway` | Supply the gateway bearer as `AVA_CLUSTER_SECRET` for the first start. |

The initialization journal binds credentials before their first effects.
Repeated or interrupted start preserves them. Do not edit the journal or copy a
new environment template over the generated file. Credential rotation is a
separate authorized operation; it is not performed by a start retry.

Runner bootstrap projects the active runner login and the Redis runtime
credential. `AVA_CLUSTER_SECRET` remains the HTTP bearer; the gateway's
database authority store and Redis-admin credential remain private to it.
