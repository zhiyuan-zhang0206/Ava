"""The closed lists of the ambient-state rule (scripts/structure/ambient_state/__init__.py).

Everything the rule lets through without a baseline entry is named here, and every
entry says why. The lists are closed: a new entry is a deliberate edit with a
one-line reason, never an inline comment at the site. Entries that name a file or a
site go stale — the gate fails when the file is gone or no longer holds the thing —
so a list cannot rot into a permission wall.

Three families are kept, and only these (user ruling 2026-10-02):

1. **Write-only facades** — loggers, meters, tracers, and the internals of the
   log/telemetry sinks. Code writes to them and nothing reads them back to decide
   what to do (`SINK_CALLEES`, `SINK_CALLEE_SUFFIXES`, `SINK_FACADES`).
2. **Framework wiring** — an `app`/router/parser/graph definition and the
   synchronization primitives. A lock is wiring; the state it protects is not
   (`WIRING_CALLEES`).
3. **Pure constants** — a value computed from literals that cannot change an
   answer: a compiled regex, a `TypeVar`, a `timedelta`, a `Path`; and a memoized
   pure function, which is a cache of a constant rather than state
   (`PURE_CALLEES`, `PURE_REPO_CALLEES`, `ALLOWED`).

`DEFERRED` is not an exemption: it annotates frozen baseline sites whose fix waits
on a separate redesign.
"""

from __future__ import annotations

# ── 1. write-only facades ──────────────────────────────────────────────────

# A handle to a logger / meter / tracer: written through, never read to decide.
SINK_CALLEES = frozenset({"logging.getLogger"})
SINK_CALLEE_SUFFIXES = (
    ".get_meter",
    ".get_tracer",
    ".create_counter",
    ".create_histogram",
    ".create_gauge",
    ".create_up_down_counter",
)

# Files that ARE a sink facade: the module-level state in them is the facade's own
# write-only machinery (buffers, cursors, handles), so none of it is reported. The
# exemption covers state only: a thread or task a facade starts is still reported.
# Path -> why.
SINK_FACADES: dict[str, str] = {
    "agent/graph/node_log.py": (
        "snapshot cursor and node-exit aggregate buffered for the log/event stream; "
        "written by node exits, read only by the flush that emits them"
    ),
    "base/log/__init__.py": (
        "the loguru sink facade: import-time reset and extra binding of the loguru global, "
        "the sample counter and the Postgres sink handle; read only by the log pipeline itself"
    ),
    "base/telemetry/emitter.py": (
        "the telemetry event pipeline: its state, failure counters and exit hook are written by "
        "emitters and read only by the pipeline itself"
    ),
    "base/telemetry/tracing.py": (
        "the tracer facade: the OTel trace API used through the global tracer provider, plus "
        "the arm state it keeps; write-only for callers"
    ),
    "base/telemetry/otlp/telemetry_otlp.py": (
        "the OTLP export backend: the meter facade over the global meter provider, the backend "
        "handle and the once-per-process export gate; write-only for callers"
    ),
    "base/telemetry/metrics/observed_metrics.py": (
        "the observed-metrics writer: its pool handle and failure counter are written by "
        "emitters and read only by the writer itself"
    ),
    "gateway/middleware/latency.py": (
        "latency aggregation buffer flushed to the event pipeline; written by requests, "
        "read only by the flush"
    ),
    "gateway/middleware/runtime_metrics.py": (
        "active-SSE-connection gauge state, incremented and decremented by the stream "
        "handler and read only by the metrics exporter"
    ),
}

# ── 2. framework wiring ────────────────────────────────────────────────────

WIRING_CALLEES = frozenset(
    {
        # The application / route table / CLI definition: declarative, built once.
        "fastapi.FastAPI",
        "fastapi.APIRouter",
        "typer.Typer",
        "argparse.ArgumentParser",
        "langgraph.graph.StateGraph",
        # Synchronization primitives: the lock is wiring, the state it guards is not.
        "threading.Lock",
        "threading.RLock",
        "threading.Event",
        "threading.Condition",
        "threading.Semaphore",
        "threading.BoundedSemaphore",
        "threading.local",
        "asyncio.Lock",
        "asyncio.Event",
        "asyncio.Semaphore",
        "asyncio.BoundedSemaphore",
        "asyncio.Condition",
    }
)

# ── 3. pure constants ──────────────────────────────────────────────────────

# Constructors whose result is fixed by their literal arguments.
PURE_CALLEES = frozenset(
    {
        # builtins over literals
        "frozenset", "tuple", "str", "int", "float", "bool", "bytes", "len", "min", "max",
        "object", "getattr", "sorted", "range", "repr", "type",
        # patterns, type declarations, enums
        "re.compile", "typing.TypeVar", "typing.ParamSpec", "typing.TypeVarTuple",
        "typing.NewType", "typing.TypedDict", "typing.NamedTuple", "typing.cast",
        "typing.Annotated", "collections.namedtuple", "enum.Enum", "enum.IntEnum",
        "enum.StrEnum", "enum.auto", "dataclasses.field", "pydantic.TypeAdapter",
        "pydantic.ConfigDict", "pydantic.Field",
        # dates, numbers, identifiers and paths built from literals
        "datetime.timedelta", "datetime.datetime", "datetime.date", "datetime.timezone",
        "decimal.Decimal", "fractions.Fraction", "uuid.UUID", "ipaddress.ip_network",
        "ipaddress.ip_address", "pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath",
        "os.path.join", "os.path.dirname", "os.path.abspath", "os.path.basename",
        # immutable views, formatters and key functions
        "types.MappingProxyType", "str.maketrans", "struct.Struct", "string.Template",
        "operator.attrgetter", "operator.itemgetter", "base64.b64decode",
        "psycopg.sql.SQL", "psycopg.sql.Identifier", "psycopg.sql.Literal",
        # configuration values of a client library, fixed at definition
        "httpx.Timeout", "httpx.Limits",
        # a declarative channel spec inside a LangGraph state schema
        "langgraph.channels.delta.DeltaChannel",
    }
)  # fmt: skip

# Constructors that are pure only when a method is called on the result at once
# (`hashlib.sha256(b"").hexdigest()`): assigned bare they would be a stateful hasher.
PURE_CHAIN_CALLEES = frozenset({"hashlib.sha256", "hashlib.sha1", "hashlib.md5"})

# Repo functions that return a constant derived from literals only (callee -> why).
# A listed function that no longer exists fails as stale.
PURE_REPO_CALLEES: dict[str, str] = {
    "base.events.contract._sql_keys": "derives a tuple of key names from the static event registry",
    "base.events.contract.family_events": "a tuple over the static event registry",
    "base.events.contract.lineage_event_names": "a tuple over the static event registry",
    "base.packages.skills.scan._rx": "compiles a regex from a literal pattern",
    "services.gateway_side.backup.passphrase.derive": "a key derivation of a literal: a fixed value",
}

# Exact sites let through, `path::rule:name` (the baseline's own key shape) -> why.
# Used for a memoized pure function: `lru_cache` over a derivation that depends on
# nothing mutable is a cache of a constant, not state. A site that stops being
# reported fails as stale.
ALLOWED: dict[str, str] = {
    "ava/mcp_config.py::hidden-singleton:_session_death_codes": "constants read from mcp.types",
    "ava/sdk_surface/discovery.py::hidden-cache:_module_ast": "the parse of one module's source: one module, one tree",
    "ava_builtins/plugins/ava_memory/sdk.py::hidden-cache:_documented_pool": "wraps one home's pool path in a documented constant",
    "base/agents/history/hierarchy/tokens.py::hidden-singleton:_encoder": "loads the fixed tiktoken vocabulary",
    "base/api_contracts/contracts.py::hidden-cache:_template_regex": "a regex compiled from a template string",
    "base/config/candidate.py::hidden-cache:_candidate_validation_model": "builds a validation subclass of a given Settings class",
    "base/config/service_read.py::hidden-singleton:domain_model_classes": "the static table of Settings domain classes",
    "base/host/env/audit.py::hidden-singleton:_load_alias_metadata": "static alias metadata of the env registry",
    "base/host/env/config_registry.py::hidden-singleton:_build_registry": "the static config registry derived from the Settings classes",
    "base/host/env/config_registry.py::hidden-singleton:field_infos": "a static view of the config registry",
    "base/host/env/registry.py::hidden-singleton:agent_runner_cluster_aliases": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:cluster_scope_aliases": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:derived_env_keys": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:env_identity_keys": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:health_port_env_aliases": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:launch_input_keys": "a static view of the env registry",
    "base/host/env/registry.py::hidden-singleton:session_forward_keys": "a static view of the env registry",
    "base/native_process/group_closure.py::hidden-singleton:_proc_listpids": "a ctypes loader of one OS entry point: code, not state",
    "base/native_process/pidfd.py::hidden-singleton:_api": "a ctypes loader of the pidfd syscalls: code, not state",
    "ava/__init__.py::ambient-instance:extend": "a namespace of functions built once and never rebound or filled afterwards",
}

# ── deferred: frozen in the baseline, fix waits on another redesign ────────

DEFERRED_WARNING_REDESIGN = "deferred: warning/alert redesign"
# Log-throttle flags ("already warned" sets, last-emitted stamps). The warning/alert
# pipeline is being redesigned as a whole; a one-off `warn_once` helper is not added
# meanwhile, and the throttle code is untouched. Same key shape as ALLOWED -> reason.
DEFERRED: dict[str, str] = {
    f"{site}": DEFERRED_WARNING_REDESIGN
    for site in (
        "agent/graph/capabilities.py::ambient-container:_warned_unresolved",
        "ava_builtins/plugins/ava_syntax_fix/_imports.py::hidden-singleton:_warn_ruff_missing_once",
        "base/daemon/health.py::ambient-container:_warned_windows_8106",
        "base/events/live/redis_client.py::ambient-container:_warn_last",
        "base/packages/plugins/enable_config.py::ambient-container:_dangling_reported",
        "base/sessions/pty/records.py::ambient-container:_retained_warning_reasons",
        "gateway/auth/rejection_log.py::global-rebind:_auth401_total",
        "gateway/auth/rejection_log.py::ambient-container:_auth401_last_warn",
        "gateway/auth/rejection_log.py::ambient-container:_auth401_suppressed",
        "gateway/cluster/_stats_dashboard.py::ambient-container:_stale_emit_at",
        "gateway/inspect/_metrics_health.py::ambient-container:_last_logged",
        "gateway/routers/fleet_graph.py::ambient-container:_stale_emit_at",
        "services/delivery_watchdog/daemon.py::ambient-container:_resurrect_suppressions",
        "services/healthchecks/permissions_helper.py::global-rebind:_reported_unhealthy",
    )
}
