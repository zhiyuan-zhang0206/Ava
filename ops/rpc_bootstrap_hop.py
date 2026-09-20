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


class BootstrapRecoveryReadPayload(BaseModel):
    """``cluster_bootstrap_recovery_read`` payload: no arguments.

    The question is about the unit's own journal slot, so the payload carries
    nothing -- the envelope's target machine is the addressing, and the answer
    is one read-only unit-local fact.
    """

    model_config = ConfigDict(extra="forbid")


class BootstrapRecoveryReadResult(BaseModel):
    """``cluster_bootstrap_recovery_read`` result: this unit's bootstrap-recovery journal slot.

    `journal_present` is the effect marker the exact pre-stop abort requires
    absent on every unit: the hop child's first durable write is this journal
    (before it, every action was staged-file writes or read-only checks).
    `journal_stage` rides along when the journal is readable, for the
    operator's diagnosis. A present-but-unreadable journal still reports
    `journal_present=True` (with no stage) -- an effect is reported as the
    fact it is, never an op failure.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    journal_present: bool
    journal_stage: str | None = Field(default=None, max_length=64)
