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
re-install. It is the gateway's human bearer (API, frontend login) and stays on
the gateway: services, agents and remote units present their write
generation's machine API token instead (`AVA_API_TOKEN`), which the gateway
admits for the active generation only, and a runner's `/ops` accepts only its
generation's tokens. It does not authenticate Postgres, PgBouncer, or Redis:
the internal data plane always authenticates with its own credentials,
whatever the bearer, nor the logical backups: their passphrase is minted and
pinned at birth (`$AVA_HOME/backups/logical-backup.passphrase`, keep it with the
backup keys). Rotate the secret only with `scripts/data_plane_ops/rotate_cluster_secret.py`;
the config API refuses to write it.

Postgres application logins are write generations recorded in the gateway's
private `$AVA_HOME/db-authority/`: one gateway and one runner login inheriting
the `NOLOGIN` groups `ava_gateway` / `ava_runner`. The schema owner is `NOLOGIN`
and the gateway `.env` holds only the credential-free endpoint. A remote runner
receives the runner login, its API token and the telemetry token only in a
sealed capability bundle the gateway operator issues for that unit (`ava cluster
db-authority issue-unit`; the login and token are shared by every runner unit of
the generation), installed into its private `$AVA_HOME/db-authority/`;
bootstrap never serves a database login or the human secret. Runner Redis has the independent `AVA_REDIS_PASSWORD`, embedded
only in the bootstrap URL; Redis `default`/`requirepass` uses
`AVA_REDIS_ADMIN_PASSWORD`, file-only on the gateway. A remote-managed plane
keeps its provider URLs and `AVA_RUNNER_DB_PASSWORD` for its gateway-local
agents; it has no write generation to issue to remote runners.

The capabilities given to `ava init` determine the initial control-plane secret:

| `ava init` shape | Secret |
|---|---|
| `--serve-gateway --serve-agent-runner` | Empty bearer by default; unauthenticated loopback API. Postgres/PgBouncer still admit only write-generation logins (generation 0 minted at first start) and Redis gets its generated admin and runtime passwords. |
| `--serve-gateway --no-serve-agent-runner` | Minted automatically. It stays on the gateway; runners receive capability bundles instead. |
| `--serve-agent-runner --no-serve-gateway` | No bearer: supply the capability bundle's transport key as `AVA_DB_CAPABILITY_KEY` for `ava init` (and again for a later `install-unit`). A runner home recording `AVA_CLUSTER_SECRET` refuses. |

The initialization journal binds credentials before their first effects.
Repeated start or an interrupted init preserves them. Do not edit the journal or copy a
new environment template over the generated file. Credential rotation is a
separate authorized operation; it is not performed by a start retry.

Runner bootstrap serves the Redis runtime credential and no database login or
human secret; the gateway's database authority store, human bearer and
Redis-admin credential remain private to it.
