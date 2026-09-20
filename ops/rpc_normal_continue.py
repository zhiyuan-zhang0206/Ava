"""``cluster_normal_continue`` op payload/result -- one unit's continuation step.

Split out of `ops/rpc_schemas.py` at the file-size budget (the
`ops/rpc_terminate.py` pattern). The op runs on a unit's ops server during a
managed-writer rollout's continuation phase (task #4129, channel E): the daemon
verifies that the payload's request path is canonical private unit state, then
starts the `ava-updater` session on the retained candidate image's
`cli.commands._update_agent_runner --normal-release|--normal-commit` entry
against it. The entry owns every authority check the continuation needs -- the
request is a request, never authority: the child re-reads the live rollout
lease and re-derives each binding locally, and its own handoff compare-and-set
refuses a live owner.

`step` selects the half: "drive" is the unit's publication drive, "commit" the
post-publication commit tail. The receipt is the spawn fact (session name +
log path), not a generation: the ledger writes belong to the continuation
itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.managed_writer_barrier import Digest


class NormalContinuePayload(BaseModel):
    """``cluster_normal_continue`` payload: the unit-local request path + step.

    `continue_request` names one existing private file inside this unit's `run/`
    directory (`{home}/run/...`, 0600, owned); `step` selects which half of the
    unit's publication tail this dispatch drives; `artifact_digest` selects the
    retained image whose interpreter runs the entry. Both locate and launch --
    the entry re-verifies the whole context from the request bytes itself.
    """

    model_config = ConfigDict(extra="forbid")

    continue_request: str = Field(min_length=1, max_length=4096)
    step: Literal["drive", "commit"]
    artifact_digest: Digest


class NormalContinueResult(BaseModel):
    """``cluster_normal_continue`` result: the spawned entry session, as facts.

    `session` is the detached `ava-updater` session's name and `log` its append
    target.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    session: str = Field(min_length=1, max_length=128)
    log: str = Field(min_length=1, max_length=4096)
