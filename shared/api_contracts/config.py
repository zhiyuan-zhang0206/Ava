"""Runtime-config API contract — the config-panel views the gateway serves and
the CLI reads/writes.

Downshifted from `gateway/schemas/config.py` so both sides of the wire can name
one type: the gateway registers these on `GET/PUT /api/config` (so they keep
their OpenAPI schema names `ConfigFieldView` / `ConfigView` /
`ConfigFieldWriteResult` / `ConfigWriteResult`), and `ava config` thin clients
validate the response against them without importing up into `gateway`.
"""

from typing import Literal

from pydantic import (
    BaseModel,
)

# The config-panel capability sections — same Literal the config metadata resolves
# (`shared.config.Capability`), reused so the schema field and the metadata it is
# built from are one type, not two structurally-equal aliases.
from shared.config import Capability


class ConfigFieldView(BaseModel):
    """View of a single config field — server-side metadata + current value.

    The server composes metadata fields and sends them down; the frontend
    does not do any "infer config back" logic — it displays + sends PUT
    back by name.

    `can_enable` / `reason` are a host-side read-time capability hint, only
    populated for host-scope fields with a static precondition (today
    `browser_enabled`: can the host actually run a headed browser). Both stay
    None for non-host fields and for host fields with no pre-grey gate (free
    text / numbers validated only at write time).
    """

    name: str
    field_type: str  # "bool" | "string" | "int" | "float" | "enum"
    current_value: object  # bool | str | int | float | None
    default_value: object
    description: str
    group: str  # owning-domain label (e.g. "LLM", "Data plane") — the second-level bucket
    # Owning machine capability — conceptual ownership + the remote-view field
    # filter (a pure agent-runner's view shows only agent-runner + common fields).
    # The panel's DISPLAY grouping is NOT derived from this: the frontend regroups
    # into finer semantic sections via its own static map (_config_groups.ts).
    # "common" = not owned by a single capability (cluster-wide policy or shared
    # host identity).
    capability: Capability
    restart_required: str  # "agent" | "ops" | "gateway" | "all" | ""
    writable: bool
    sensitive: bool
    env_var: str
    scope: str  # "cluster-pinned" | "cluster-default" | "host" | "agent"
    # Whether a spawn/restart config overlay may override this field per agent —
    # drives the panel's "per-agent" tag and filter (one layer above `explicit`:
    # per-process, not per-cluster). Mirrors ResolvedFieldView.per_agent.
    per_agent: bool
    remote_writable: bool
    # For an "enum" field, the allowed values (Literal members) the frontend
    # renders as a select; None for every other field_type.
    choices: list[str] | None = None
    can_enable: bool | None = None
    reason: str | None = None


class ConfigView(BaseModel):
    """GET /api/config response — grouped field list + raw_overrides (PUT body source).

    raw_overrides is config.json's current content — the frontend deltas
    against this and returns the result via PUT.

    machine_capabilities is the target machine's capability set (`gateway` and/or
    `agent-runner`) — the gateway's own for the Cluster (self) view, the `?machine=`
    host's for a remote view. The panel uses it to pick which capability sections to
    render on a remote view: a pure agent-runner shows only its agent-runner + common
    sections, while a co-located gateway,agent-runner box shows the gateway section too
    (so its gateway-daemon toggles stay editable). The Cluster view always shows all
    sections regardless (it edits every capability's cluster fields).
    """

    fields: list[ConfigFieldView]
    raw_overrides: dict[str, object]
    # The machine's capability tokens — a subset of {gateway, agent-runner}
    # (machine_role() / machines.role); never "common", which is a config bucket,
    # not a machine capability.
    machine_capabilities: list[Literal["gateway", "agent-runner"]]


class ResolvedFieldView(BaseModel):
    """One per-model-defaultable setting resolved for a specific model.

    The read-only mirror of `shared/lm/registry.py:explain_setting`: the value an
    agent on this model boots with, plus every candidate layer and the name of
    the one that won. There is no write path here — an explicit value is edited
    as the normal config field of the same `name`, so the panel links back to
    that row rather than growing a second store.
    """

    name: str  # flat config field name — the same key GET/PUT /api/config uses
    env_var: str
    description: str
    field_type: str  # "bool" | "string" | "int" | "float" | "enum"
    choices: list[str] | None = None
    group: str  # owning-domain label, same as ConfigFieldView.group
    effective_value: object
    # Which layer produced effective_value. "explicit" = the user pinned it
    # (`.env` / exported env); the two defaults are code (registry.py).
    source: Literal["explicit", "model-default", "shared-default"]
    explicit_value: object  # None iff the user did not pin it
    model_default: object  # None iff this model has no per-model opinion
    shared_default: object  # the DEFAULT_TUNING floor; never None
    # Whether a spawn/restart config overlay may override this field per agent —
    # the one layer ABOVE `explicit` that this view cannot see (it is per
    # process, not per cluster).
    per_agent: bool
    restart_required: str


class ResolvedConfigView(BaseModel):
    """GET /api/config/resolved response — every tunable resolved for one model.

    `model` echoes the model actually resolved against (the cluster's own
    `llm_model` when the request omitted it). `registered` is False for a model
    id absent from the registry: resolution still works (it simply has no
    per-model layer, exactly as at runtime), and the flag lets the panel say so
    instead of presenting shared defaults as if the model had chosen them.
    """

    model: str
    registered: bool
    fields: list[ResolvedFieldView]


class ConfigFieldWriteResult(BaseModel):
    """Per-field verdict of a PUT /api/config write — `ok` plus a human-readable
    `reason` when the field was rejected (capability / scope / unknown).

    `reason` is None iff ok. The host-side op is authoritative for host-scope
    fields (re-validates remote_writable + capability); cluster fields can't
    fail a capability check, so they don't appear here on a self PUT.
    """

    ok: bool
    reason: str | None = None


class ConfigWriteResult(BaseModel):
    """PUT /api/config response — per-field results + whether anything was applied.

    `applied` is True iff every field passed and the write committed (atomic:
    one bad field -> nothing written). `restart_required` is the union of the
    written fields' restart targets ("agent" | "ops" | "gateway" | "all"), for
    the per-machine "needs restart" banner.
    """

    applied: bool
    results: dict[str, ConfigFieldWriteResult]
    restart_required: list[str]
