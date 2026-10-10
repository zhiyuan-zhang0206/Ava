"""Boot-lite configuration owned by a ConfigBoot instance.

Owners prepare at their process boundary and build the eager chain on first
non-lite use. Each owns its lock, pending overlays and stable view. The existing
module facade remains a temporary adapter during complete consumer migration.
Lite parsing, environment delivery and the bounded/re-entrant full-build gate
retain their previous timing.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, cast

from base.config.profiles import (
    AVA_PROCESS_PROFILE_ENV,
    PROCESS_PROFILES,
    ProcessProfile,
    profile_domain_error,
    profile_unknown_error,
)
from base.host.env.bootstrap import (
    CONFIG_FETCH_ENV,
    CONFIG_FETCH_SKIP,
    config_source_is_local,
    should_fetch_from_gateway,
)
from base.host.env.config_lite_table import (
    FIELD_ALIASES,
    FIELD_DOMAINS,
    LITE_FIELDS,
    REQUIRED_FIELDS,
)
from base.host.env.config_registry import DOMAIN_ATTRS
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL, EnvBootResult, load_ava_env

# `AVA_CONFIG_BOOT=eager` — the operator's instant rollback to the eager boot.
BOOT_MODE_ENV = "AVA_CONFIG_BOOT"
BOOT_MODE_EAGER = "eager"

_LITE = "lite"
_FULL = "full"

# Internal invariant (task #3696 exception inventory): bound for a read
# waiting on another thread's in-flight eager build. Not config: the wait runs
# before the config chain exists (reading a field would itself trigger the
# upgrade), and the value is fixed at 3x the bootstrap fetch bound
# (base/host/env/bootstrap.py _FETCH_TIMEOUT_S = 10s) -- the slowest legitimate
# segment of a build. A slower build degrades to the retryable
# ConfigBuildWaitTimeoutError, so no operator knob is warranted.
_BUILD_WAIT_TIMEOUT_SECONDS = 30.0

# The one upgrade lock: prepare, the build, and the install run under it.


# The never-dialed placeholder for the required redis URL (mirrors the eager
# boot path); the db placeholder is PLACEHOLDER_DB_URL, which base/db
# refuses with an actionable error.
_LITE_REDIS_URL = "redis://config-lite@127.0.0.1:1/0"


#
# The parse kinds mirror pydantic's non-strict coercion for the field types the
# table declares (bool strings, int from a trimmed integer literal or an
# integral decimal, float, Path, the CSV split of the eval allowlist). The
# parity test (base/tests/config/bootstrap/test_config_lite_parity.py) locks every kind
# against the eager sub-model construction, error for error.

_BOOL_TRUE = frozenset({"1", "true", "yes", "on", "t", "y"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off", "f", "n"})
# Python's `int()` accepts the optional sign and underscores; pydantic's int
# additionally accepts a plain decimal that is integral ("5.0", not "5.", not
# ".5", not "5e1") — and rejects non-ASCII digits (Python would accept them).
_INT_LITERAL = re.compile(r"[+-]?[0-9][0-9_]*\Z")
_INT_DECIMAL = re.compile(r"[+-]?[0-9]+\.[0-9]+\Z")


def _parse_bool(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ValueError("not a boolean")


def _parse_int(raw: str) -> int:
    text = raw.strip()
    if _INT_LITERAL.match(text):
        return int(text)
    if _INT_DECIMAL.match(text):
        value = float(text)
        if value.is_integer():
            return int(value)
    raise ValueError("not an integer")


def _check_iana(alias: str, value: Any) -> None:
    """Mirror of GeneralSettings._validate_timezone (same message)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{alias}={value!r} is not a valid IANA timezone name "
            f"(e.g. America/Los_Angeles, Asia/Shanghai, UTC)"
        ) from exc


def _check_port(alias: str, value: Any) -> None:
    """Mirror of telemetry_otlp_port's Field(ge=1, le=65535)."""
    if not 1 <= value <= 65535:
        raise ValueError(f"{alias}={value!r} must be a TCP port between 1 and 65535")


def _check_eval_allowlist(alias: str, value: Any) -> None:
    """Mirror of AgentEvalSettings._validate_eval_network_allowlist (same message)."""
    unsupported = sorted(set(value) - {"web", "understand"})
    if unsupported:
        raise ValueError(
            f"{alias}: eval network allowlist only accepts 'web' and 'understand'; "
            f"unsupported entries: {unsupported}"
        )


_CHECKS = {
    "iana": _check_iana,
    "port": _check_port,
    "eval_allowlist": _check_eval_allowlist,
}
_PARSE_KINDS = frozenset({"str", "int", "float", "bool", "bool_or_none", "path", "csv_str_list"})


def _parse(name: str, alias: str, kind: str, check: str | None, raw: str) -> Any:
    """Coerce one env value to the field's type — ValueError on a bad value."""
    if (
        kind not in _PARSE_KINDS
    ):  # pragma: no cover - the table is generated; a new kind adds a parser
        raise ValueError(f"unknown parse kind {kind!r}")
    try:
        if kind == "str":
            value: Any = raw
        elif kind == "int":
            value = _parse_int(raw)
        elif kind == "float":
            value = float(raw.strip())
        elif kind in ("bool", "bool_or_none"):
            value = _parse_bool(raw)
        elif kind == "path":
            value = Path(raw)
        else:
            value = [item.strip() for item in raw.split(",") if item.strip()]
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{alias}={raw!r} is not a valid {kind} for config field {name!r} "
            f"(boot-lite parse; the field type is declared in base/config/)"
        ) from exc
    if check is not None:
        _CHECKS[check](alias, value)
    return value


def _default_value(owner: ConfigBoot, name: str, default_kind: str, literal: Any) -> Any:
    """The field's default, per the named default kind recorded in the table."""
    if default_kind == "literal":
        return literal
    if default_kind == "none":
        return None
    if default_kind == "factory_empty_list":
        return []
    if default_kind == "otel_endpoint_from_port":
        # Mirrors ObservabilitySettings._default_local_otlp_endpoint: when the
        # endpoint itself is not explicitly set, it follows the port.
        return f"http://127.0.0.1:{owner.resolve('telemetry_otlp_port')}"
    raise ValueError(
        f"unknown default kind {default_kind!r} for config field {name!r}"
    )  # pragma: no cover


class _DomainView:
    """Read proxy for one config domain while the process is lite.

    A field listed in the static index for this domain resolves in place; any
    other attribute is the eager sub-model's business and upgrades. After an
    upgrade the proxy keeps working: it delegates to the real sub-model."""

    __slots__ = ("_domain", "_owner")

    _owner: ConfigBoot
    _domain: str

    def __init__(self, owner: ConfigBoot, domain: str) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_domain", domain)

    def __getattr__(self, name: str) -> Any:
        domain = self._domain
        if self._owner.mode == _FULL:
            return getattr(getattr(self._owner._current_settings(), domain), name)
        if name in self._owner.pending and FIELD_DOMAINS.get(name) == domain:
            return self._owner.pending[name]
        row = LITE_FIELDS.get(name)
        if row is not None and row[0] == domain:
            return self._owner.resolve(name)
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if not self._owner._maybe_upgrade(f"settings.{domain}.{name}"):
            raise AttributeError(_build_window_message(f"settings.{domain}.{name}"))
        return getattr(getattr(self._owner._current_settings(), domain), name)

    def __setattr__(self, name: str, value: Any) -> None:
        domain = object.__getattribute__(self, "_domain")
        if self._owner.mode != _FULL:
            self._owner.upgrade(f"settings.{domain}.{name} assignment")
        setattr(getattr(self._owner._current_settings(), domain), name, value)


class _SettingsView:
    """The stable `settings` object: lite until the first eager touch, then the
    constructed singleton. The object identity never changes, so every
    `from base.config import settings` binding — and every `set_field`
    write — keeps working across the upgrade."""

    __slots__ = ("_domains", "_owner")

    _owner: ConfigBoot
    _domains: dict[str, _DomainView]

    def __init__(self, owner: ConfigBoot) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_domains", {})

    @property
    def profile(self) -> str | None:
        if self._owner.mode == _FULL:
            return self._owner._current_settings().profile
        self._owner.prepare()
        return self._owner.profile

    def has_domain(self, name: str) -> bool:
        """Whether this process's profile constructs the `name` config domain.

        The escape hatch for dynamic code (plugins) that must probe before
        reading; static code should simply access `settings.<domain>` and let
        the fail-fast AttributeError point at the fix.
        """
        if self._owner.mode == _FULL:
            return bool(self._owner._current_settings().has_domain(name))
        self._owner.prepare()
        profile = self._owner.profile
        if profile is None:
            return True
        return name in PROCESS_PROFILES[profile]

    def __getattr__(self, name: str) -> Any:
        if self._owner.mode == _FULL:
            return getattr(self._owner._current_settings(), name)
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name in DOMAIN_ATTRS:
            self._owner._check_domain_allowed(name)
            domains: dict[str, _DomainView] = object.__getattribute__(self, "_domains")
            view = domains.get(name)
            if view is None:
                view = domains[name] = _DomainView(self._owner, name)
            return view
        if not self._owner._maybe_upgrade(f"settings attribute {name!r}"):
            raise AttributeError(_build_window_message(f"settings.{name}"))
        return getattr(self._owner._current_settings(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if self._owner.mode != _FULL:
            self._owner.upgrade(f"settings.{name} assignment")
        setattr(self._owner._current_settings(), name, value)


def _build_window_message(target: str) -> str:
    return f"config boot-lite: {target} is not readable while the eager config build is in flight"


class ConfigBuildWaitTimeoutError(AttributeError):
    """A read waited out the bounded window for another thread's eager build.

    Retryable: the other thread's build keeps running (or a fresh attempt runs
    after a failed one), and nothing is corrupted by the wait itself. Inherits
    AttributeError so attribute-read callers keep their previous catch shape
    for window conditions.
    """


def _build_wait_timeout_message(target: str) -> str:
    return (
        f"config boot-lite: {target} is not readable yet: the in-flight eager config "
        f"build exceeded the {_BUILD_WAIT_TIMEOUT_SECONDS:g}s wait bound; retry shortly"
    )


def _require_local_fields() -> None:
    """W1: a local-source unit fails fast when a required field has no value.

    A field with no default (the data-plane URLs) is filled from the unit's
    `.env` or the process env; with neither, the eager boot raised a pydantic
    "Field required" at construction. The boot-lite boot must keep that
    fail-fast at the same point — the check is local-branch-only by design: a
    runner's values arrive from the gateway fetch (validated after it), and
    skip/bare processes get placeholders.
    """
    missing = [
        name for name in sorted(REQUIRED_FIELDS) if os.environ.get(FIELD_ALIASES[name]) is None
    ]
    if missing:
        named = ", ".join(f"{FIELD_ALIASES[name]} ({name})" for name in missing)
        raise ValueError(
            f"required config missing for this local-source unit: {named} — its .env is the "
            f"config source (config_source_is_local()) and no value is present in the "
            f"environment. Set it in $AVA_HOME/.env or the process environment."
        )


def _plant_placeholders() -> None:
    """Plant never-dialed placeholders for the required data-plane URLs so a
    construction succeeds without a gateway fetch. A value already in env/.env
    is left alone — the placeholder only fills the nothing-at-all case."""
    os.environ.setdefault("AVA_DB_URL", PLACEHOLDER_DB_URL)
    os.environ.setdefault("AVA_REDIS_URL", _LITE_REDIS_URL)


def _log_upgrade(reason: str) -> None:
    # Debug-only observability (Q6): the event is already visible in
    # `boot_state()` for tests and acceptance runs.
    if "loguru" not in sys.modules:
        return
    from base.log import logger

    logger.debug("config boot-lite upgraded to the eager config chain (reason={!r})", reason)


class ConfigBoot:
    """One composition owner's lazy configuration, overlays and build gate.

    Construction performs no environment delivery or full-model build. Call
    boot() at the existing process entry; pass view or narrow reader closures
    to consumers. A view always resolves this instance, including after upgrade.
    """

    __slots__ = (
        "_lock",
        "env_boot",
        "mode",
        "pending",
        "prepared",
        "profile",
        "reason",
        "settings",
        "upgrades",
        "upgrading",
        "upgrading_thread",
        "view",
    )

    def __init__(self) -> None:
        self._lock = RLock()
        self.view = _SettingsView(self)
        self.env_boot: EnvBootResult | None = None
        self.mode = _LITE
        self.reason: str | None = None
        self.upgrades = 0
        self.upgrading = False
        self.upgrading_thread: int | None = None
        self.prepared = False
        self.profile: ProcessProfile | None = None
        # Overlay writes made in lite mode, in insertion order: name -> raw value.
        self.pending: dict[str, Any] = {}
        # This owner's constructed model, set by _install().
        self.settings: Any = None

    def _current_settings(self) -> Any:
        return self.settings

    def _apply_cluster_timezone(self) -> None:
        from base.config import _apply_timezone_name

        name = self.get_field("timezone") if self.field_explicitly_set("timezone") else None
        _apply_timezone_name(name)

    def refresh_data_plane_settings(self) -> None:
        """Refresh only this owner's data-plane slice at the existing call point."""
        from base.config._full import refresh_data_plane_settings

        self.upgrade("refresh_data_plane_settings()")
        refresh_data_plane_settings(self._current_settings())

    def resolve(self, name: str) -> Any:
        """Resolve one `LITE_FIELDS` entry: pending override > env > default."""
        self.prepare()
        if name in self.pending:
            return self.pending[name]
        _domain, alias, kind, default_kind, literal, check = LITE_FIELDS[name]
        raw = os.environ.get(alias)
        if raw is not None:
            return _parse(name, alias, kind, check, raw)
        return _default_value(self, name, default_kind, literal)

    def field_explicitly_set(self, name: str) -> bool:
        """Whether `name` was explicitly provided to this process rather than
        defaulted — the lite equivalent of `model_fields_set` for a boot-built
        singleton: its env alias is present (the `.env` load ran), or an overlay
        pinned it (`set_field`; pydantic records a post-init setattr in
        `model_fields_set` too, so both modes agree). `cluster_tz_name` is built
        on this: the field default alone does not make a value authoritative."""
        self.prepare()
        if self.mode == _FULL:
            return name in getattr(self._current_settings(), FIELD_DOMAINS[name]).model_fields_set
        return name in self.pending or os.environ.get(FIELD_ALIASES[name]) is not None

    def _check_domain_allowed(self, domain: str) -> None:
        """The lite half of the profile fail-fast (the eager half lives in
        `Settings.__getattr__`; both raise the same message)."""
        profile = self.profile
        if profile is not None and domain not in PROCESS_PROFILES[profile]:
            raise profile_domain_error(profile, domain)

    def get_field(self, name: str) -> Any:
        """Current value of a leaf field by name, resolved to its owning sub-model.

        The escape hatch for reflective / dynamic access — a flat
        `getattr(settings, name)` no longer works now that fields live on
        `settings.<domain>`. Static access should use the nested attribute directly
        (`settings.lm.llm_state.model`); this is for call sites that hold the field name as a
        runtime string (health-port map, model-key map, capability probes)."""
        domain = FIELD_DOMAINS[name]
        if self.mode == _FULL:
            return getattr(getattr(self._current_settings(), domain), name)
        self._check_domain_allowed(domain)
        if name in self.pending:
            return self.pending[name]
        if name in LITE_FIELDS:
            return self.resolve(name)
        if self.upgrading and self.upgrading_thread != get_ident():
            self._wait_for_in_flight_build(f"get_field({name!r})")
        self.upgrade(f"get_field({name!r})")
        return getattr(getattr(self._current_settings(), domain), name)

    def set_field(self, name: str, value: Any) -> None:
        """Set a field on this owner; existing views/readers see the same model.

        Used by the per-agent config overlay at process boot.

        In lite mode ANY registered field is recorded as a pending override and
        replayed onto the constructed singleton at upgrade; reads see it already
        (pending resolves ahead of the index), so an overlay write never forces the
        eager chain — the hosted exec child writes the agent's whole frozen set,
        most of which the boot-path index does not carry (#3621 BLK-1). An unknown
        name is a KeyError, and a domain outside the profile fails fast, both as in
        the eager path."""
        domain = FIELD_DOMAINS[name]
        if self.mode == _FULL:
            setattr(getattr(self._current_settings(), domain), name, value)
            return
        self._check_domain_allowed(domain)
        self.pending[name] = value

    def prepare(self) -> None:
        """Make the boot-lite state resolvable — once per process.

        Runs at the `base.config` import tail for every non-skip process (the
        settings-lite maintenance verbs keep their deferred load) and at the first
        config read for a skip process. Mirrors the eager boot's env work exactly:

        1. `load_ava_env()` — the `.env` load + the env-authority force/drop pass;
        2. the config-source decision: a local unit must hold its required
           data-plane values (W1 check), a configured pure runner fetches
           `GET /api/bootstrap` (the same import-time fetch, and the same
           BootstrapFetchError when the gateway is unreachable), everything else
           gets the never-dialed placeholders so a later construction succeeds;
        3. every boot-path field that is present in the environment parses and
           validates now — the import-time fail-fast for `LITE_FIELDS`;
        4. the cluster clock is applied (`apply_cluster_timezone`).
        """
        if self.prepared:
            return
        with self._lock:
            if self.prepared:
                return
            # Latched first: a re-entrant read during step 4 (cluster_tz_name reads
            # through the view) must not prepare again.
            self.prepared = True
            self.env_boot = load_ava_env()
            profile = os.environ.get(AVA_PROCESS_PROFILE_ENV)
            if profile is not None and profile not in PROCESS_PROFILES:
                raise profile_unknown_error(profile)
            self.profile = cast(ProcessProfile, profile)
            self._apply_source_decision()
            for name, row in LITE_FIELDS.items():
                raw = os.environ.get(row[1])
                if raw is not None:
                    _parse(name, row[1], row[2], row[5], raw)
            self._apply_cluster_timezone()

    def _apply_source_decision(self) -> None:
        """The eager boot's source decision, minus the construction it guarded.

        `_plant_placeholders` keeps the eager boot's env side effect (the
        never-dialed URLs) so a later `Settings()` construction finds the same
        environment it would have found eagerly."""
        if config_source_is_local():
            _require_local_fields()
        elif os.environ.get(CONFIG_FETCH_ENV) == CONFIG_FETCH_SKIP:
            _plant_placeholders()
        elif should_fetch_from_gateway():
            from base.host.env.bootstrap import inject_config_from_gateway

            self.env_boot = inject_config_from_gateway()
        else:
            _plant_placeholders()

    def upgrade(self, reason: str) -> Any:
        """Build this owner's eager chain once.

        Under the upgrade lock: prepare if not yet prepared, build the full state
        (`_full.build()` — the sub-models, registry and `Settings` singleton from
        the prepared environment), install it on this owner, replay the pending
        overlay writes in insertion order, and latch `full`. Idempotent: every later
        call returns the constructed singleton. The reason (a short caller tag)
        lands in the debug log and `boot_state()`."""
        with self._lock:
            if self.mode == _FULL:
                return self.settings
            self.prepare()
            self.upgrading = True
            self.upgrading_thread = get_ident()
            try:
                from base.config._full import build

                env_boot = self.env_boot
                if env_boot is None:
                    raise RuntimeError("config boot did not produce an environment delivery result")
                bundle = build(env_boot=env_boot)
                self._install(bundle, reason)
                for name, value in tuple(self.pending.items()):
                    setattr(getattr(self.settings, FIELD_DOMAINS[name]), name, value)
                self.pending.clear()
            finally:
                self.upgrading = False
                self.upgrading_thread = None
        _log_upgrade(reason)
        return self.settings

    def _install(self, bundle: Any, reason: str) -> None:
        self.settings = bundle.settings
        self.mode = _FULL
        self.prepared = True
        self.reason = reason
        self.upgrades += 1

    def _wait_for_in_flight_build(self, reason: str) -> None:
        """Bounded wait for another thread's in-flight eager build.

        The builder holds `self._lock` across the whole upgrade (prepare, build,
        install, overlay replay), so acquiring it IS the wait. On acquisition the
        chain is installed and the caller can serve the full value; if the
        in-flight attempt died before installing, this thread runs the build
        itself so the real error surfaces. On expiry the read raises the
        retryable ConfigBuildWaitTimeoutError -- the other thread's build keeps running
        and installs when it finishes, so a retry is the right move."""
        if not self._lock.acquire(timeout=_BUILD_WAIT_TIMEOUT_SECONDS):
            raise ConfigBuildWaitTimeoutError(_build_wait_timeout_message(reason))
        try:
            if self.mode != _FULL:
                self.upgrade(reason)
        finally:
            self._lock.release()

    def _maybe_upgrade(self, reason: str) -> bool:
        """Upgrade unless THIS thread is the one already building.

        A read that arrives while another thread's build is in flight waits for it
        (bounded) and then serves the full chain. Only a read made by the building
        thread itself -- which must not wait on its own build -- returns False, and
        the caller serves the lite value or raises the documented window error."""
        if self.mode == _FULL:
            return True
        if self.upgrading:
            if self.upgrading_thread == get_ident():
                return False
            self._wait_for_in_flight_build(reason)
            return True
        self.upgrade(reason)
        return True

    def ensure_eager(self) -> Any:
        """Construct the full config chain now, at this point in the process.

        The explicit full-validation entry point for processes that must fail fast
        on every field at boot (the gateway, the ops daemons, the agent host — the
        callers whitelisted for #3621): `import base.config` alone no longer
        constructs every domain."""
        return self.upgrade("ensure_eager()")

    def boot(self) -> None:
        """The `base.config` import tail: prepare, or go eager now.

        `AVA_CONFIG_BOOT=eager` is the operator's instant rollback to the eager
        boot (and the pytest conftest's default); any other explicit value is a
        typo and fails fast rather than silently booting lite."""
        mode = os.environ.get(BOOT_MODE_ENV)
        if mode is not None and mode != BOOT_MODE_EAGER:
            raise ValueError(
                f"{BOOT_MODE_ENV}={mode!r} is not a known boot mode; must be {BOOT_MODE_EAGER!r} "
                f"or unset (the unset default is the lazy boot-lite chain)"
            )
        if os.environ.get(CONFIG_FETCH_ENV) == CONFIG_FETCH_SKIP:
            # skip wins over eager: a settings-lite maintenance verb must stay
            # repairable (a broken .env must not be able to block the tool that
            # fixes it), even when the operator's rollback flag sits in the unit
            # environment (#3621 / 405 ruling).
            return
        if mode == BOOT_MODE_EAGER:
            self.upgrade(f"{BOOT_MODE_ENV}={BOOT_MODE_EAGER}")
        else:
            self.prepare()

    def is_full(self) -> bool:
        """Whether this process has upgraded to the eager config chain."""
        return self.mode == _FULL

    def boot_state(self) -> dict[str, Any]:
        """Private test/acceptance hook: the boot-lite state machine snapshot."""
        return {
            "mode": self.mode,
            "reason": self.reason,
            "upgrades": self.upgrades,
            "pending_count": len(self.pending),
            "prepared": self.prepared,
        }


class _LegacyConfigBoot(ConfigBoot):
    """Temporary adapter for the unmigrated facade, with its existing bindings."""

    __slots__ = ()

    def _current_settings(self) -> Any:
        from base.config import settings

        return settings

    def _apply_cluster_timezone(self) -> None:
        from base.config import apply_cluster_timezone

        apply_cluster_timezone()

    def _install(self, bundle: Any, reason: str) -> None:
        facade = sys.modules["base.config"]
        facade.__dict__.update(bundle.exports)
        facade.__dict__["settings"] = bundle.settings
        super()._install(bundle, reason)


# The existing legacy owner remains until every consumer has an explicit input.
_state = _LegacyConfigBoot()


def resolve(name: str) -> Any:
    return _state.resolve(name)


def field_explicitly_set(name: str) -> bool:
    return _state.field_explicitly_set(name)


def get_field(name: str) -> Any:
    return _state.get_field(name)


def set_field(name: str, value: Any) -> None:
    _state.set_field(name, value)


def prepare() -> None:
    _state.prepare()


def upgrade(reason: str) -> Any:
    return _state.upgrade(reason)


def _maybe_upgrade(reason: str) -> bool:
    return _state._maybe_upgrade(reason)


def ensure_eager() -> None:
    _state.ensure_eager()


def boot() -> None:
    _state.boot()


def is_full() -> bool:
    return _state.is_full()


def boot_state() -> dict[str, Any]:
    return _state.boot_state()


def settings_view() -> _SettingsView:
    return _state.view


def refresh_data_plane_settings() -> None:
    _state.refresh_data_plane_settings()
