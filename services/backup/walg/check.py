"""`ava backup walg check`: prove, before archiving is switched on, that it can work.

The failures this exists to surface early: a missing or edited binary, an unusable
configuration or key, and a storage credential that can write but not read or
delete (retention only deletes weeks later, so a missing Delete permission would
otherwise show up long after the setup was forgotten). The storage steps are one
round trip through WAL-G's own storage tools, under the configured prefix:
`st put` a small object, `st ls` it, `st get` it back and compare (the encryption
round trip), `st rm` it, `st ls` again to see it gone. Then, when Postgres answers,
the archive facts it reports are printed (informational: the health probe judges them).

`st put` always compresses, so the stored object is `<name>.lz4` and `st get`
takes that full name. Nothing printed here contains a credential or key value.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from base.cluster.dataplane import walg_binary
from services.backup.walg import config as walg_config
from services.backup.walg import probe
from services.backup.walg.archive import expected_archive
from services.backup.walg.runner import WalgCommandError, run_walg

_STORAGE_CALL_TIMEOUT_S = 120
_CHECK_DIR = "ava-check"


@dataclass(frozen=True)
class Step:
    name: str
    detail: str
    ok: bool = field(default=True, kw_only=True)


def _storage_round_trip() -> str:
    token = uuid.uuid4().hex
    remote = f"{_CHECK_DIR}/{token}.txt"
    stored = f"{remote}.lz4"
    payload = f"ava walg check {token}\n".encode()

    def call(*args: str) -> str:
        return str(run_walg(list(args), timeout_s=_STORAGE_CALL_TIMEOUT_S))

    with tempfile.TemporaryDirectory(prefix="ava-walg-check-") as scratch:
        source, fetched = Path(scratch, "put"), Path(scratch, "get")
        source.write_bytes(payload)
        try:
            call("st", "put", str(source), remote)
            if token not in call("st", "ls", f"{_CHECK_DIR}/"):
                raise WalgCommandError("the object just written is not listed")
            call("st", "get", stored, str(fetched))
            if fetched.read_bytes() != payload:
                raise WalgCommandError("the object read back differs from the one written")
        finally:
            # Always try to leave nothing behind, then prove the delete permission.
            removal_error: WalgCommandError | None = None
            try:
                call("st", "rm", stored)
            except WalgCommandError as exc:
                removal_error = exc
        if removal_error is not None:
            raise WalgCommandError(
                f"cannot delete (is Delete granted on the prefix?): {removal_error}"
            )
        if token in call("st", "ls", f"{_CHECK_DIR}/"):
            raise WalgCommandError("the object is still listed after it was deleted")
    return "put, list, get (encryption round trip), delete, list"


def _postgres_facts() -> str:
    expected = expected_archive()
    try:
        with probe.admin_connection() as conn:
            state = probe.read_archiver_state(conn)
    except Exception as exc:
        return f"not read ({type(exc).__name__}); the health probe judges it once Postgres is up"
    if expected is not None and probe.settings_differ(state, expected):
        verdict = "differs from the configuration (ava stop, then ava start, to apply)"
    else:
        verdict = "matches the configuration"
    return (
        f"archive_mode={state.archive_mode}, {verdict}; "
        f"archived={state.archived_count} failed={state.failed_count}"
    )


def run_check() -> list[Step]:
    """Run the checks in order, stopping at the first failure; the last step is the verdict."""
    path = walg_config.configured_path()
    if path is None:
        return [Step("configured", "AVA_WALG_CONFIG_FILE is not set", ok=False)]
    steps = [Step("configured", str(path))]

    binary_problem = walg_binary.installed_problem()
    if binary_problem is not None:
        return [*steps, Step("binary", f"{binary_problem} (ava converge installs it)", ok=False)]
    steps.append(Step("binary", f"pinned wal-g {walg_binary.WALG_VERSION}"))

    try:
        config = walg_config.read_config(path)
        key_problem = walg_config.pin_problem(config)
    except walg_config.WalgConfigError as exc:
        return [*steps, Step("configuration", str(exc), ok=False)]
    if key_problem is not None:
        return [*steps, Step("configuration", key_problem, ok=False)]
    pinned = (
        "pinned"
        if walg_config.pinned_key_id() == config.key_fingerprint
        else "not pinned yet: converge pins it on first use"
    )
    steps.append(
        Step(
            "configuration",
            f"prefix {config.prefix}; key fingerprint {config.key_fingerprint} ({pinned}); "
            "WALG_PREVENT_WAL_OVERWRITE on",
        )
    )

    try:
        steps.append(Step("storage", _storage_round_trip()))
    except WalgCommandError as exc:
        return [*steps, Step("storage", str(exc), ok=False)]

    return [*steps, Step("postgres", _postgres_facts())]
