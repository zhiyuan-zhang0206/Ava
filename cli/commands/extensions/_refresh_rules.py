"""Pure rules of the package refresh pass: cadence (duration, backoff, jitter, due-time),
`git ls-remote` parsing and the staged-tree gates.

Split out of `packages_refresh.py` (the pass itself) so each decision reads on its own and the
engine module stays within the file budget.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.deploy.git import host_version
from base.packages.plugins import manifest as manifest_module
from base.packages.skills import scan

# Backoff: failures double the effective interval, capped after this many
# doublings (so a repeatedly failing package still re-checks about weekly).
_BACKOFF_MAX_DOUBLINGS = 5
_BACKOFF_CAP_SECONDS = 7 * 24 * 3600


def _jitter_percent(name: str, last_check_at: str | None) -> int:
    """Deterministic +/-10% jitter token for one package's interval.

    Deterministic (keyed on the name + last check stamp) so two runs agree on
    when a package is due; the point is only that fleet machines do not all
    check at the same instant of their interval.
    """
    digest = hashlib.sha256(f"{name}|{last_check_at or ''}".encode()).digest()
    return (digest[0] % 21) - 10


def effective_interval_seconds(
    base_seconds: int, failures: int, *, name: str, last_check_at: str | None
) -> int:
    """`base` doubled per consecutive failure (capped), plus +/-10% jitter."""
    seconds = min(base_seconds * (2 ** min(failures, _BACKOFF_MAX_DOUBLINGS)), _BACKOFF_CAP_SECONDS)
    return max(1, int(seconds * (100 + _jitter_percent(name, last_check_at)) / 100))


def _parse_stamp(stamp: str | None) -> datetime | None:
    if stamp is None:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_due(
    last_check_at: str | None,
    interval_seconds: int,
    failures: int,
    *,
    name: str,
    now: datetime,
) -> bool:
    """Whether a package's next check is due — never checked = due now."""
    last = _parse_stamp(last_check_at)
    if last is None:
        return True
    eff = effective_interval_seconds(
        interval_seconds, failures, name=name, last_check_at=last_check_at
    )
    return now >= last + timedelta(seconds=eff)


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _sha_of(line: str) -> str | None:
    sha = line.split("\t", 1)[0].strip()
    return sha if _SHA_RE.match(sha) else None


def core_head_from_ls_remote(stdout: str, ref: str) -> str | None:
    """The sha of `ref` in the core channel's `ls-remote` output (branch match, else any line)."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    for line in lines:
        on_ref = line.rstrip().endswith(f"refs/heads/{ref}") or line.rstrip().endswith(f"\t{ref}")
        if on_ref and (sha := _sha_of(line)):
            return sha
    for line in lines:
        if sha := _sha_of(line):
            return sha
    return None


def _remote_head(lines: list[str]) -> str | None:
    for line in lines:
        if line.rstrip().endswith("\tHEAD") and (sha := _sha_of(line)):
            return sha
    return None


def _branch_sha(lines: list[str], ref: str) -> str | None:
    heads = [line for line in lines if line.rstrip().endswith(f"refs/heads/{ref}")]
    return _sha_of(heads[0]) if heads else None


def _tag_sha(lines: list[str], ref: str) -> str | None:
    """The tag's sha; an annotated tag's peeled (`^{}`) commit wins."""
    tags = [
        line
        for line in lines
        if line.rstrip().endswith(f"refs/tags/{ref}")
        or line.rstrip().endswith(f"refs/tags/{ref}^{{}}")
    ]
    if not tags:
        return None
    peeled = [line for line in tags if line.rstrip().endswith("^{}")]
    return _sha_of((peeled or tags)[0])


def git_head_from_ls_remote(
    stdout: str, ref: str | None, source: str
) -> tuple[str | None, bool, str | None]:
    """`(sha, pinned, error)` of one git-channel package from its `ls-remote` output.

    No `ref` tracks the remote HEAD; a branch head moves (`pinned` False); a tag, or a bare
    sha, is pinned.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if ref is None:
        head = _remote_head(lines)
        return (head, False, None) if head else (None, False, "no HEAD on the remote")
    if sha := _branch_sha(lines, ref):
        return sha, False, None
    if sha := _tag_sha(lines, ref):
        return sha, True, None
    if _SHA_RE.match(ref):
        return ref, True, None
    return None, False, f"ref {ref!r} not found on {source}"


def staged_gate_error(staged: Path, repo: Path) -> str | None:
    """The refusal result for a staged tree that fails a gate, or None when it may be applied.

    Gates in order: it carries a SKILL.md, no critical scan finding, a valid manifest, and
    (when it has a manifest) a host engine / commit the manifest accepts.
    """
    if not any(staged.rglob("SKILL.md")):
        return "error: staged tree carries no SKILL.md"
    critical = scan.criticals(scan.scan_package(staged))
    if critical:
        return f"refused_scan: {', '.join(scan.rule_ids(critical))}"
    try:
        manifest = manifest_module.load_manifest(staged)
    except manifest_module.ManifestError as exc:
        return f"error: manifest invalid: {exc}"
    if manifest is None:
        return None
    host_errors: list[str] = []
    try:
        host = host_version.host_version(repo)
    except host_version.HostVersionError as exc:
        host_errors.append(str(exc))
    else:
        host_errors += manifest_module.check_host_engine(manifest, host)
    host_errors += manifest_module.check_host_commit(manifest, repo)
    if host_errors:
        return f"blocked_version: {'; '.join(host_errors)}"
    return None
