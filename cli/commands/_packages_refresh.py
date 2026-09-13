"""Package refresh executor — one pass that makes this machine's channel-backed
skills match their sources (design `future/infra/core-package-update-channel.md`
§5.3; tasks #2915 / #3267).

The engine behind `ava packages refresh`. One code path for manual runs and the
per-machine OS job (`--from-job` adds the job-only gates). It NEVER writes the
checkout — the core channel fetches objects only (`git fetch`, FETCH_HEAD +
object store, never the working tree, the same contract as
`shared.cluster_drift.prod_source_fetch`) — and content lands only under
`$AVA_HOME/skills/` via the same staged-swap + preserved-subtree machinery the
converge path uses. It never restarts anything (activation is the next skill
scan) and never passes `--accept-risk`: the scan gate refuses, nothing else.

Skip conditions (job runs only: `AVA_OS_JOBS_ENABLED`, `refresh_enabled`;
always: another pass holds the per-home flock, a cluster update is in flight,
the registry is unreadable).

Legacy rows carry no `applied_rev`. The design infers "the checkout's installed
commit"; this pass instead reconciles by CONTENT: it stages the remote head and
compares against the on-disk tree (`.<name>.new` vs `<name>`) — equal content
advances `applied_rev` with no write, different content goes through the gates
and applies. Same outcome for a consistent machine, and it also catches a disk
stale relative to the checkout (e.g. the rollout window where the skip rule
took over before a first refresh ran).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from cli.commands._converge_skills import _Source, iter_sources
from shared import host_version, install_registry, paths, plugin_manifest, skill_scan
from shared.config import settings
from shared.gitenv import git_env
from shared.os_cron import os_jobs_enabled
from shared.platform import LockTimeoutError, file_lock
from shared.proc import run_bounded
from shared.skill_names import match_key

# Backoff: failures double the effective interval, capped after this many
# doublings (so a repeatedly failing package still re-checks about weekly).
_BACKOFF_MAX_DOUBLINGS = 5
_BACKOFF_CAP_SECONDS = 7 * 24 * 3600
# How long the pass waits for the per-home flock before skipping (a held lock
# means another pass is live; waiting is pointless — this pass is periodic).
_QUEUE_LOCK_TIMEOUT_S = 1.0
# Local (non-network) git calls: diff / archive / cat-file on the checkout.
_LOCAL_GIT_TIMEOUT_S = 30.0
# Fallback check cadence when a channel-backed row resolves no interval.
_DEFAULT_INTERVAL_S = 86400

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def parse_duration(text: str) -> int:
    """Seconds from `30s` / `15m` / `24h` / `7d` (bare number = seconds)."""
    match = _DURATION_RE.match(text)
    if match is None:
        raise ValueError(f"duration {text!r} must look like 30s / 15m / 24h / 7d")
    return max(1, int(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"])


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


@dataclass(frozen=True)
class ItemOutcome:
    """One package's pass result (`result` uses the registry's last_result
    vocabulary: up_to_date | applied | available: … | blocked_version: … |
    conflict: … | refused_scan: … | error: …)."""

    name: str
    kind: str
    channel: str
    mode: str
    result: str


@dataclass(frozen=True)
class RefreshReport:
    """The pass's whole outcome — what ran, what each package did, counts, notes."""

    ran: bool
    skip_reason: str | None
    channel_line: str | None
    items: tuple[ItemOutcome, ...]
    counts: dict[str, int]
    notes: tuple[str, ...] = ()


@dataclass
class _Delta:
    """Registry write-back staged during the pass, applied in ONE mutate at the end."""

    update: dict[str, object] = field(default_factory=dict[str, object])
    package: dict[str, object] = field(default_factory=dict[str, object])


class _Pass:
    """One refresh pass. Filesystem work happens before any registry write; the
    write-back is a single locked read-modify-write (`mutate`) at the end."""

    def __init__(
        self,
        *,
        check_only: bool,
        only: str | None,
        force: bool,
        from_job: bool,
        now: datetime,
        repo: Path | None,
    ) -> None:
        self.check_only = check_only
        self.only = only
        self.force = force
        self.from_job = from_job
        self.now = now
        self.repo = repo or paths.repo_root()
        self.skills_dir = paths.skills_dir()
        self.registry: install_registry.Registry | None = None
        self.deltas: dict[str, _Delta] = {}
        self.channel: install_registry.ChannelState | None = None
        self.core_head: str | None = None
        self.core_note: str | None = None
        self.items: list[ItemOutcome] = []
        self.notes: list[str] = []
        self.counts: dict[str, int] = {}
        self.applies_used = 0
        self.deadline = 0.0
        self._sources_cache: list[_Source] | None = None

    # -- bookkeeping ---------------------------------------------------------

    def _count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _record(self, pkg: install_registry.InstalledPackage, result: str) -> None:
        """Stage the standard per-package write-back for one pass outcome."""
        delta = self.deltas.setdefault(pkg.name, _Delta())
        delta.update["last_check_at"] = self.now.isoformat(timespec="seconds")
        delta.update["last_result"] = result
        self._keep_resolved(pkg, delta)
        ok = result in {"up_to_date", "applied"} or result.startswith("available")
        if ok:
            delta.update["failures"] = 0
        else:
            delta.update["failures"] = pkg.update.failures + 1
        policy = install_registry.resolved_policy(pkg)
        self.items.append(
            ItemOutcome(
                name=pkg.name,
                kind=pkg.type,
                channel=policy.channel or "-",
                mode=policy.mode,
                result=result,
            )
        )
        self._count(result.split(":", 1)[0])

    def _keep_resolved(self, pkg: install_registry.InstalledPackage, delta: _Delta) -> None:
        """Freeze the resolved policy into the row at first sight (§5.2)."""
        policy = install_registry.resolved_policy(pkg)
        if pkg.update.channel is None and policy.channel is not None:
            delta.update["channel"] = policy.channel
        if pkg.update.mode is None:
            delta.update["mode"] = policy.mode
        if pkg.update.interval_seconds is None and policy.interval_seconds is not None:
            delta.update["interval_seconds"] = policy.interval_seconds

    # -- engine --------------------------------------------------------------

    def run(self) -> RefreshReport:
        try:
            registry = install_registry.load()
        except (install_registry.InstallRegistryError, OSError) as exc:
            return RefreshReport(
                ran=False,
                skip_reason=f"registry unreadable: {exc}",
                channel_line=None,
                items=(),
                counts={},
            )
        self.registry = registry
        self.deadline = time.monotonic() + settings.packages.refresh_budget_seconds

        core: list[install_registry.InstalledPackage] = []
        git_pkgs: list[install_registry.InstalledPackage] = []
        for pkg in registry.packages:
            if self.only is not None and match_key(pkg.name) != match_key(self.only):
                continue
            policy = install_registry.resolved_policy(pkg)
            skip_why: str | None = None
            if policy.channel is None:
                skip_why = "no channel (local / hand-registered package)"
            elif policy.mode == "off":
                skip_why = "update policy is off"
            elif pkg.type != "skill":
                skip_why = f"apply for type={pkg.type} lands in P2"
            elif self.from_job and not is_due(
                pkg.update.last_check_at,
                policy.interval_seconds or _DEFAULT_INTERVAL_S,
                pkg.update.failures,
                name=pkg.name,
                now=self.now,
            ):
                self._count("skipped_not_due")
                continue
            if skip_why is not None:
                if self.only is not None:
                    self.notes.append(f"'{pkg.name}': {skip_why}")
                continue
            if policy.channel == "core":
                core.append(pkg)
            else:
                git_pkgs.append(pkg)
        if self.only is not None and not core and not git_pkgs and not self.notes:
            self.notes.append(f"no tracked channel-backed skill named '{self.only}'")

        if core:
            self._resolve_core_channel(core, registry)
            if self.core_head is None:
                # Channel resolution failed — every package was recorded above.
                core = []
        core_keys = {p.name for p in core}
        queue = sorted(core + git_pkgs, key=lambda p: (p.update.last_check_at or "", p.name))
        for idx, pkg in enumerate(queue):
            if self.applies_used >= settings.packages.refresh_max_applies:
                self._count("skipped_budget", len(queue) - idx)
                break
            if time.monotonic() > self.deadline:
                self._count("skipped_budget", len(queue) - idx)
                break
            if pkg.name in core_keys:
                self._process_core(pkg, self.core_head)
            else:
                self._process_git(pkg)

        try:
            self._commit()
        except LockTimeoutError as exc:  # registry lock contended: records not landed
            logger.warning("packages refresh: registry write-back skipped: {}", exc)
        return RefreshReport(
            ran=True,
            skip_reason=None,
            channel_line=self.core_note,
            items=tuple(self.items),
            counts=dict(self.counts),
            notes=tuple(self.notes),
        )

    # -- core channel --------------------------------------------------------

    def _checkout_remote(self) -> str | None:
        result = run_bounded(
            ["git", "-C", str(self.repo), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_LOCAL_GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
        remote = result.stdout.strip()
        return remote or None

    def _ls_remote(self, remote: str, ref: str) -> tuple[str | None, str | None]:
        """(`head sha`, `error`) for the core channel's ref on `remote`."""
        try:
            result = run_bounded(
                ["git", "-C", str(self.repo), "ls-remote", remote, ref],
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=settings.packages.refresh_network_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"ls-remote failed: {exc}"
        if result.returncode != 0:
            return None, f"ls-remote failed: {_tail(result.stderr)}"
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        for line in lines:
            if line.rstrip().endswith(f"refs/heads/{ref}") or line.rstrip().endswith(f"\t{ref}"):
                sha = line.split("\t", 1)[0].strip()
                if _SHA_RE.match(sha):
                    return sha, None
        for line in lines:
            sha = line.split("\t", 1)[0].strip()
            if _SHA_RE.match(sha):
                return sha, None
        return None, f"ref {ref!r} not found on {remote}"

    def _fetch_core(self, remote: str, ref: str) -> str | None:
        """Objects-only fetch (never the working tree); `error` or None."""
        try:
            result = run_bounded(
                ["git", "-C", str(self.repo), "fetch", "--no-tags", remote, ref],
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=settings.packages.refresh_network_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"fetch failed: {exc}"
        if result.returncode != 0:
            return f"fetch failed: {_tail(result.stderr)}"
        return None

    def _resolve_core_channel(
        self,
        core_pkgs: list[install_registry.InstalledPackage],
        registry: install_registry.Registry,
    ) -> None:
        """Resolve `core@<ref>` head once per pass; fetch when it moved or a
        package still needs objects. Failures are recorded per due package."""
        stamp = self.now.isoformat(timespec="seconds")
        state = registry.channels.get("core")
        remote = (state.remote_url if state else None) or self._checkout_remote()
        ref = (state.ref if state else None) or "main"
        if remote is None:
            self.core_note = "core: no 'origin' remote on the checkout"
            for pkg in core_pkgs:
                self._record(pkg, "error: core channel has no 'origin' remote on the checkout")
            return
        head, err = self._ls_remote(remote, ref)
        if err is not None or head is None:
            self.core_note = f"core@{ref}: {err}"
            for pkg in core_pkgs:
                self._record(pkg, f"error: core channel check failed — {err}")
            self.channel = install_registry.ChannelState(
                name="core",
                remote_url=remote,
                ref=ref,
                last_seen_sha=state.last_seen_sha if state else None,
                last_checked_at=stamp,
                last_result=f"error: {err}",
            )
            return
        need_objects = any(
            p.update.applied_rev is None or p.update.applied_rev != head or p.update.failures > 0
            for p in core_pkgs
        )
        last_seen = state.last_seen_sha if state else None
        note = f"core@{ref} head {head[:7]}"
        if head != last_seen or need_objects:
            fetch_err = self._fetch_core(remote, ref)
            if fetch_err is not None:
                self.core_note = f"{note} — {fetch_err}"
                for pkg in core_pkgs:
                    self._record(pkg, f"error: core channel fetch failed — {fetch_err}")
                self.channel = install_registry.ChannelState(
                    name="core",
                    remote_url=remote,
                    ref=ref,
                    last_seen_sha=last_seen,
                    last_checked_at=stamp,
                    last_result=f"error: {fetch_err}",
                )
                return
            last_seen = head
            note += " (fetched)"
        self.core_head = head
        self.core_note = note
        self.channel = install_registry.ChannelState(
            name="core",
            remote_url=remote,
            ref=ref,
            last_seen_sha=last_seen,
            last_checked_at=stamp,
            last_result=note,
        )

    def _sources(self) -> list[_Source]:
        if self._sources_cache is None:
            sources, _conflicts = iter_sources(self.repo)
            self._sources_cache = sources
        return self._sources_cache

    def _source_rel(self, pkg: install_registry.InstalledPackage) -> tuple[str | None, str | None]:
        """The package's repo-relative source path, or an error string."""
        want = match_key(pkg.name)
        src: Path | None = None
        for source in self._sources():
            if match_key(source.name) == want:
                src = source.src
                break
        if src is None and pkg.origin == "plugin" and pkg.origin_path:
            candidate = Path(pkg.origin_path)
            if candidate.is_dir() and candidate.is_relative_to(self.repo):
                src = candidate
        if src is None:
            return (
                None,
                "source path not in the checkout (removal is converge's cleanup, not refresh's)",
            )
        try:
            rel = src.relative_to(self.repo)
        except ValueError:
            return None, f"source {src} lives outside the checkout"
        return rel.as_posix(), None

    def _process_core(self, pkg: install_registry.InstalledPackage, head: str | None) -> None:
        if head is None:  # channel resolution failed; the package was recorded then
            return
        policy = install_registry.resolved_policy(pkg)
        applied = pkg.update.applied_rev
        if applied == head and pkg.update.failures == 0:
            self._record(pkg, "up_to_date")
            return
        src_rel, src_err = self._source_rel(pkg)
        if src_rel is None:
            self._record(pkg, f"error: {src_err}")
            return
        if applied is not None:
            rc, out = self._git_local("diff", "--name-only", applied, head, "--", src_rel)
            if rc == 0 and not out.strip():
                delta = self.deltas.setdefault(pkg.name, _Delta())
                delta.update["applied_rev"] = head
                self._record(pkg, "up_to_date")
                return
        if self.check_only or policy.mode == "notify":
            self._record(pkg, f"available: {head[:7]}")
            return
        staged, stage_err = self._stage_core(pkg, head, src_rel)
        if staged is None:
            self._record(pkg, f"error: {stage_err}")
            return
        result, new_hash = self._apply_staged(pkg, staged, head)
        if result in ("applied", "up_to_date"):
            delta = self.deltas.setdefault(pkg.name, _Delta())
            delta.update["applied_rev"] = head
            if result == "applied":
                stamp = self.now.isoformat(timespec="seconds")
                delta.update["last_apply_at"] = stamp
                delta.package["content_hash"] = new_hash
                delta.package["installed_hash"] = new_hash
                delta.package["updated_at"] = stamp
        self._record(pkg, result)

    def _git_local(self, *args: str) -> tuple[int, str]:
        result = run_bounded(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_LOCAL_GIT_TIMEOUT_S,
        )
        return result.returncode, result.stdout

    def _stage_core(
        self, pkg: install_registry.InstalledPackage, head: str, src_rel: str
    ) -> tuple[Path | None, str | None]:
        """Materialize `src_rel` at `head` into `skills/.<name>.new` (staged)."""
        rc, _ = self._git_local("cat-file", "-e", f"{head}:{src_rel}")
        if rc != 0:
            return None, f"path {src_rel!r} missing at {head[:7]}"
        staged = self.skills_dir / f".{pkg.name}.new"
        if staged.exists():
            shutil.rmtree(staged)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{pkg.name}.staging-", dir=self.skills_dir
        ) as tmp_str:
            tmp = Path(tmp_str)
            tarpath = tmp / "pkg.tar"
            result = run_bounded(
                ["git", "-C", str(self.repo), "archive", "-o", str(tarpath), head, "--", src_rel],
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=_LOCAL_GIT_TIMEOUT_S,
            )
            if result.returncode != 0 or not tarpath.is_file():
                return None, f"git archive failed: {_tail(result.stderr)}"
            extracted = tmp / "x"
            extracted.mkdir()
            with tarfile.open(tarpath) as tf:
                tf.extractall(extracted, filter="data")
            root = extracted / src_rel
            if not root.is_dir():
                return None, f"archive carried no directory at {src_rel!r}"
            shutil.move(str(root), str(staged))
        return staged, None

    def _apply_staged(
        self, pkg: install_registry.InstalledPackage, staged: Path, remote_rev: str
    ) -> tuple[str, str | None]:
        """Gates + staged swap for an already-materialized tree.

        Returns `(result, new content hash)`; the hash is set only when the
        swap landed. Any gate failure returns the result string and leaves the
        disk untouched."""
        dest = self.skills_dir / pkg.name
        skip = install_registry.preserved_subpaths(dest)
        dest_hash = install_registry.tree_hash(dest, skip_subtrees=skip) if dest.exists() else None
        staged_hash = install_registry.tree_hash(staged)
        if dest_hash is not None and staged_hash == dest_hash:
            return "up_to_date", None
        if not any(staged.rglob("SKILL.md")):
            return "error: staged tree carries no SKILL.md", None
        findings = skill_scan.scan_package(staged)
        critical = skill_scan.criticals(findings)
        if critical:
            return f"refused_scan: {', '.join(skill_scan.rule_ids(critical))}", None
        try:
            manifest = plugin_manifest.load_manifest(staged)
        except plugin_manifest.ManifestError as exc:
            return f"error: manifest invalid: {exc}", None
        if manifest is not None:
            host_errors: list[str] = []
            try:
                host = host_version.host_version(self.repo)
            except host_version.HostVersionError as exc:
                host_errors.append(str(exc))
            else:
                host_errors += plugin_manifest.check_host_engine(manifest, host)
            host_errors += plugin_manifest.check_host_commit(manifest, self.repo)
            if host_errors:
                return f"blocked_version: {'; '.join(host_errors)}", None
        recorded = pkg.installed_hash or pkg.content_hash
        if (
            dest_hash is not None
            and not self.force
            and install_registry.copy_changed(dest, recorded, skip_subtrees=skip)
        ):
            return "conflict: local copy differs from the last applied content", None
        for parts in skip:
            sub = dest.joinpath(*parts)
            if sub.exists():
                moved_to = staged.joinpath(*parts)
                moved_to.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(sub), str(moved_to))
        prev = self.skills_dir / f".{pkg.name}.prev"
        if prev.exists():
            shutil.rmtree(prev)
        if dest.exists():
            shutil.move(str(dest), str(prev))
        shutil.move(str(staged), str(dest))
        self.applies_used += 1
        logger.info("packages refresh: applied '{}' at {}", pkg.name, remote_rev[:7])
        return "applied", install_registry.tree_hash(dest, skip_subtrees=skip)

    # -- git channel ---------------------------------------------------------

    def _ls_remote_git(self, source: str, ref: str | None) -> tuple[str | None, bool, str | None]:
        """(`sha`, `pinned`, `error`) for one git-channel package."""
        args = ["git", "ls-remote", source, ref] if ref else ["git", "ls-remote", source, "HEAD"]
        try:
            result = run_bounded(
                args,
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=settings.packages.refresh_network_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, False, f"ls-remote failed: {exc}"
        if result.returncode != 0:
            return None, False, f"ls-remote failed: {_tail(result.stderr)}"
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if ref is None:
            for line in lines:
                if line.rstrip().endswith("\tHEAD"):
                    sha = line.split("\t", 1)[0].strip()
                    if _SHA_RE.match(sha):
                        return sha, False, None
            return None, False, "no HEAD on the remote"
        heads = [line for line in lines if line.rstrip().endswith(f"refs/heads/{ref}")]
        if heads:
            sha = heads[0].split("\t", 1)[0].strip()
            if _SHA_RE.match(sha):
                return sha, False, None
        tags = [
            line
            for line in lines
            if line.rstrip().endswith(f"refs/tags/{ref}")
            or line.rstrip().endswith(f"refs/tags/{ref}^{{}}")
        ]
        if tags:
            peeled = [line for line in tags if line.rstrip().endswith("^{}")]
            sha = (peeled or tags)[0].split("\t", 1)[0].strip()
            if _SHA_RE.match(sha):
                return sha, True, None
        if _SHA_RE.match(ref):
            return ref, True, None
        return None, False, f"ref {ref!r} not found on {source}"

    def _process_git(self, pkg: install_registry.InstalledPackage) -> None:
        from cli.commands._pkg_source import (
            SourcePathNotFoundError,
            acquire_source,
            cleanup_temp,
            looks_like_local_path,
        )

        policy = install_registry.resolved_policy(pkg)
        source = pkg.source
        if not source:
            self._record(pkg, "error: no recorded source to check")
            return
        if looks_like_local_path(source):
            self._record(pkg, "error: local-path source has no remote channel")
            return
        sha, pinned, err = self._ls_remote_git(source, pkg.ref)
        if err is not None or sha is None:
            self._record(pkg, f"error: {err}")
            return
        applied = pkg.update.applied_rev
        if applied == sha:
            self._record(pkg, "up_to_date")
            return
        if pinned and applied is not None:
            self._record(
                pkg,
                f"error: pinned ref {pkg.ref!r} now resolves to {sha[:7]} (was "
                f"{(applied or '?')[:7]}); pinned refs never auto-advance",
            )
            return
        if self.check_only or policy.mode == "notify":
            self._record(pkg, f"available: {sha[:7]}")
            return
        acquired = None
        try:
            try:
                acquired = acquire_source(source, pkg.ref)
            except (subprocess.CalledProcessError, SourcePathNotFoundError) as exc:
                detail = (
                    exc.stderr.strip()
                    if isinstance(exc, subprocess.CalledProcessError)
                    else str(exc)
                )
                self._record(pkg, f"error: {detail}")
                return
            root = acquired.root / pkg.path if pkg.path else acquired.root
            if not root.is_dir():
                self._record(pkg, f"error: path {pkg.path!r} not found in source")
                return
            staged = self.skills_dir / f".{pkg.name}.new"
            if staged.exists():
                shutil.rmtree(staged)
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                root, staged, ignore=shutil.ignore_patterns(*install_registry.IGNORED_NAMES)
            )
            result, new_hash = self._apply_staged(pkg, staged, sha)
            if result in ("applied", "up_to_date"):
                delta = self.deltas.setdefault(pkg.name, _Delta())
                delta.update["applied_rev"] = sha
                if result == "applied":
                    stamp = self.now.isoformat(timespec="seconds")
                    delta.update["last_apply_at"] = stamp
                    delta.package["content_hash"] = new_hash
                    delta.package["installed_hash"] = new_hash
                    delta.package["updated_at"] = stamp
            self._record(pkg, result)
        finally:
            if acquired is not None:
                cleanup_temp(acquired.cloned)

    # -- write-back ----------------------------------------------------------

    def _commit(self) -> None:
        """One locked read-modify-write for every staged delta, applied per
        name against the freshly-read registry (never a stale full save)."""
        if self.channel is None and not self.deltas:
            return
        with install_registry.mutate() as registry:
            if self.channel is not None:
                registry.channels["core"] = self.channel
            for key, delta in self.deltas.items():
                row = next(
                    (p for p in registry.packages if match_key(p.name) == match_key(key)), None
                )
                if row is None:
                    continue
                for field_name, value in delta.update.items():
                    setattr(row.update, field_name, value)
                for field_name, value in delta.package.items():
                    setattr(row, field_name, value)


def _tail(text: str, limit: int = 200) -> str:
    lines = (text or "").strip().splitlines()
    tail = lines[-1] if lines else ""
    return tail[-limit:]


def _skip(reason: str) -> RefreshReport:
    return RefreshReport(ran=False, skip_reason=reason, channel_line=None, items=(), counts={})


def run_refresh(
    *,
    check_only: bool = False,
    only: str | None = None,
    force: bool = False,
    from_job: bool = False,
    now: datetime | None = None,
    repo: Path | None = None,
) -> RefreshReport:
    """Run one refresh pass; never raises for expected conditions.

    `from_job` adds the job-only gates (os jobs enabled, refresh enabled,
    due-time + backoff). A manual run checks every channel-backed package
    regardless of cadence; `--check` never stages or applies; `force` overrides
    the local-edit guard (human-only — the job never passes it)."""
    moment = now or datetime.now(UTC)
    if from_job and not os_jobs_enabled():
        return _skip("OS jobs disabled (AVA_OS_JOBS_ENABLED=false)")
    if from_job and not settings.packages.refresh_enabled:
        return _skip("refresh disabled (AVA_PACKAGES_REFRESH_ENABLED=false)")
    from cli.commands.status import _update_in_flight

    lock_path = paths.ava_home() / "packages-refresh.lock"
    try:
        with file_lock(lock_path, timeout_s=_QUEUE_LOCK_TIMEOUT_S):
            if _update_in_flight():
                return _skip("a cluster update is in flight")
            pass_ = _Pass(
                check_only=check_only,
                only=only,
                force=force,
                from_job=from_job,
                now=moment,
                repo=repo,
            )
            return pass_.run()
    except LockTimeoutError:
        return _skip("another refresh pass holds the lock")
