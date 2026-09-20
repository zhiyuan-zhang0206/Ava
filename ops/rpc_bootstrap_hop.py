"""``cluster_bootstrap_hop`` op payload/result -- one unit's restricted hop start.

Split out of `ops/rpc_schemas.py` at the file-size budget (the
`ops/rpc_terminate.py` pattern). The op runs on a unit's ops server during a
managed-writer rollout's hop phase (task #4129, channel C): the daemon verifies
that the payload's request path is canonical private unit state, then starts the
`ava-updater` session on the retained candidate image's
`cli.commands._update_agent_runner --bootstrap-hop` entry. The entry owns every
authority check the hop needs -- the request is a request, never authority: the
child re-reads the live rollout lease and re-derives each binding locally
(`prepare_bootstrap_hop`), and its own handoff CAS refuses a live owner.

The receipt is the spawn fact (session name + log path), not a generation: only
the child's CAS produces a generation, so the coordinator's C-3 generation
binding reads it from the I5 ledger instead (recorded in `i4-nits-for-i5.md`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from shared.managed_writer_barrier import Digest


class BootstrapHopPayload(BaseModel):
    """``cluster_bootstrap_hop`` payload: the unit-local request path.

    `hop_request` names one existing private file inside this unit's `run/`
    directory (`{home}/run/...`, 0600, owned); `artifact_digest` selects the
    retained image whose interpreter runs the hop. Both locate and launch --
    the entry re-verifies the whole context from the request bytes itself.
    """

    model_config = ConfigDict(extra="forbid")

    hop_request: str = Field(min_length=1, max_length=4096)
    artifact_digest: Digest


class BootstrapHopResult(BaseModel):
    """``cluster_bootstrap_hop`` result: the spawned hop session, as facts.

    `session` is the detached `ava-updater` session's name and `log` its append
    target. No generation here: only the child's own CAS produces one.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    session: str = Field(min_length=1, max_length=128)
    log: str = Field(min_length=1, max_length=4096)
