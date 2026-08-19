"""OTel Collector sidecar install — pinned binary download + config generation.

One collector per machine (task #1266): every agent on the host sends OTLP to
its 127.0.0.1:4318 (OTLP/HTTP); the collector fans out to Tempo/Loki/Prometheus and
mirrors traces to local JSONL. This module is the converge step's engine —
idempotent: an already-installed, version-matching binary is a no-op; the
config is regenerated on every converge so fan-out/retention setting changes
propagate on the next `ava start`.

The CONTRIB distribution is required, not core: the file exporter (JSONL
mirror) and the file_storage extension (persistent sending queue) live in
contrib. The version is pinned to the collector release the LGTM stack
(deploy/lgtm) was validated with, and each platform's release tarball is
SHA256-pinned.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from string import Template
from urllib.parse import unquote, urlsplit

from cli.commands._converge_spec import ConvergeCtx
from shared.machine import MachineRoles

# Pinned contrib version — re-validate against the deploy/lgtm backends
# (Tempo/Loki/Prometheus OTLP intake) when bumping.
OTELCOL_CONTRIB_VERSION = "0.155.0"

# SHA256 of each supported platform's release tarball
# (opentelemetry-collector-releases v0.155.0 checksums). Keyed by the platform
# tag used in the asset name.
_OTELCOL_CONTRIB_SHA256: dict[str, str] = {
    "darwin_arm64": "dc9ecd5243bc632a2901d028bfc7a705fb6317b269c9514b5f8187b80caf8c56",
    "darwin_amd64": "7a792b03c6c7d8cfa1b67c08fd9d0c5c021a1387f926ecba92b9cfbec2d0f80e",
    "linux_amd64": "229cfddeb0621d2a011bfd1c8894335479e46349b93a0cfbccbe653443a3ec95",
    "linux_arm64": "f2fac079d5b761a729e49ba5db319cab92affc558406dc42e3c1c08e0827e14f",
    "windows_amd64": "c34e1e064641956f16f2089d1384657ecef43dd64407da0516948f786fd045a8",
}

_DOWNLOAD_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/"
    f"releases/download/v{OTELCOL_CONTRIB_VERSION}/"
    "otelcol-contrib_{version}_{tag}.tar.gz"
)

_VERSION_MARKER = "version"


def platform_tag() -> str | None:
    """The release asset tag for this machine, or None when unsupported.

    Tags follow the release assets: darwin_arm64 / darwin_amd64 / linux_amd64 /
    linux_arm64 / windows_amd64. Anything else (linux_386, windows_arm64, ...)
    has no pinned binary — the sidecar is skipped and OTLP export auto-disables
    at the agent preflight.
    """
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if arch is None:
        return None
    if platform.system() == "Darwin":
        return f"darwin_{arch}"
    if platform.system() == "Linux":
        return f"linux_{arch}"
    if platform.system() == "Windows":
        return "windows_amd64" if arch == "amd64" else None
    return None


def _binary_name() -> str:
    return "otelcol-contrib.exe" if platform.system() == "Windows" else "otelcol-contrib"


def _config_template(repo: Path) -> str:
    return (repo / "deploy/otel-collector/otel-collector.yaml").read_text(encoding="utf-8")


def _yaml_quote(value: str) -> str:
    """A single-quoted YAML scalar — the form that needs no escaping but ''."""
    return "'" + value.replace("'", "''") + "'"


# The data-plane receiver block, rendered into $DATA_PLANE_RECEIVERS. Scrape
# interval is 60s (not host_metrics' 30s): connection counts and redis memory
# are slow-moving pressure gauges, and each scrape opens a real backend
# session on a pooler-fronted Postgres.
_DATA_PLANE_BLOCK = """
  # This cluster's OWN Postgres — dialed DIRECT, never through PgBouncer: the
  # receiver reads pg_stat_* views, which a transaction-pooled session cannot
  # be trusted to serve consistently. The role is the cluster's NOSUPERUSER
  # owner, so the receiver collects what that role can see (its own database's
  # stats); metrics needing pg_monitor are simply absent rather than fatal.
  postgresql:
    endpoint: {pg_endpoint}
    transport: tcp
    username: {pg_user}
    password: {pg_password}
    databases: [{pg_database}]
    collection_interval: 60s
    tls:
      insecure: true

  # This cluster's OWN Redis. Password is the cluster secret (requirepass ==
  # the secret); an empty secret is the no-auth single-box default and the
  # receiver simply skips AUTH.
  redis:
    endpoint: {redis_endpoint}
    transport: tcp
    password: {redis_password}
    collection_interval: 60s
"""


def _endpoint(url: str, what: str) -> str:
    """`host:port` for a receiver, or an explosion naming what to fix.

    A connection URL with no port cannot be scraped, and guessing the default
    would point the receiver at whatever else listens there.
    """
    parts = urlsplit(url)
    if parts.hostname is None or parts.port is None:
        raise RuntimeError(
            f"cannot build the otel-collector {what} receiver: {what} URL has no "
            f"host:port (got {parts.scheme}://{parts.netloc!r})"
        )
    return f"{parts.hostname}:{parts.port}"


def _data_plane_receivers(roles: MachineRoles | None) -> tuple[str, str]:
    """(receiver block, pipeline-list fragment) for this unit's own data plane.

    Empty pair on anything that does not own Postgres+Redis: a pure
    agent-runner's URLs point at the GATEWAY's data plane, so scraping from
    there would duplicate the gateway's own series under a second `host`
    label. An unconfigured unit (roles None) has no URLs to read at all.
    """
    if roles is None or "gateway" not in roles:
        return "", ""
    from shared.config import settings
    from shared.db import UNANCHORED_DB_SENTINEL, direct_db_url

    db_url = direct_db_url()
    if db_url == UNANCHORED_DB_SENTINEL:
        return "", ""
    pg = urlsplit(db_url)
    redis_url = settings.data_plane.redis_url
    block = _DATA_PLANE_BLOCK.format(
        pg_endpoint=_endpoint(db_url, "postgres"),
        pg_user=_yaml_quote(unquote(pg.username or "")),
        pg_password=_yaml_quote(unquote(pg.password or "")),
        pg_database=_yaml_quote(pg.path.lstrip("/")),
        redis_endpoint=_endpoint(redis_url, "redis"),
        redis_password=_yaml_quote(unquote(urlsplit(redis_url).password or "")),
    )
    return block, ", postgresql, redis"


def generate_config(repo: Path, ava_home: Path, roles: MachineRoles | None) -> str:
    """Render the sidecar config from the repo template + this unit's settings."""
    from shared.config import settings

    obs = settings.observability
    loki_base = obs.telemetry_loki_url.rstrip("/") + "/otlp"
    prom_base = obs.telemetry_prometheus_url.rstrip("/") + "/api/v1/otlp"
    data_plane_block, data_plane_pipeline = _data_plane_receivers(roles)
    return Template(_config_template(repo)).substitute(
        AVA_HOME=str(ava_home),
        TEMPO_ENDPOINT=obs.telemetry_tempo_endpoint.rstrip("/"),
        LOKI_BASE=loki_base,
        PROM_BASE=prom_base,
        RETENTION_DAYS=str(obs.trace_retention_days),
        DATA_PLANE_RECEIVERS=data_plane_block,
        DATA_PLANE_PIPELINE_RECEIVERS=data_plane_pipeline,
    )


def _download_and_verify(tag: str, dest_dir: Path) -> None:
    """Download + SHA256-verify + extract the pinned tarball into dest_dir."""
    url = _DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag)
    expected = _OTELCOL_CONTRIB_SHA256[tag]
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        print(f"  · otel-collector: downloading {url}")
        urllib.request.urlretrieve(url, tarball)  # noqa: S310 — pinned https release asset
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(
                f"otelcol-contrib {OTELCOL_CONTRIB_VERSION} {tag} SHA256 mismatch: "
                f"got {digest}, expected {expected} — refusing to install"
            )
        with tarfile.open(tarball) as tf:
            members = [m for m in tf.getmembers() if m.name.endswith(_binary_name())]
            if not members:
                raise RuntimeError(f"otelcol-contrib tarball has no {_binary_name()} member")
            tf.extract(members[0], path=tmp)
        extracted = Path(tmp) / members[0].name
        dest = dest_dir / _binary_name()
        shutil.move(str(extracted), dest)
        if platform.system() != "Windows":
            dest.chmod(0o755)
    (dest_dir / _VERSION_MARKER).write_text(OTELCOL_CONTRIB_VERSION + "\n", encoding="utf-8")


def ensure_otel_collector(repo: Path, ava_home: Path, roles: MachineRoles | None) -> None:
    """Idempotent install: pinned binary present + config regenerated.

    Binary download is skipped when the version marker matches. Config is
    ALWAYS regenerated (template changes and setting changes propagate on the
    next converge). Unsupported platforms warn and skip — the sidecar session
    will not start and the agent preflight disables OTLP export with a
    reported warning.
    """
    tag = platform_tag()
    if tag is None:
        print(
            f"  ! otel-collector: no pinned otelcol-contrib for this platform "
            f"({platform.system()} {platform.machine()}) — sidecar skipped",
            file=sys.stderr,
        )
        return
    dest_dir = ava_home / "otel-collector"
    dest_dir.mkdir(parents=True, exist_ok=True)
    binary = dest_dir / _binary_name()
    marker = dest_dir / _VERSION_MARKER
    if not (
        binary.exists() and marker.read_text(encoding="utf-8").strip() == OTELCOL_CONTRIB_VERSION
    ):
        _download_and_verify(tag, dest_dir)
        print(f"  · otel-collector: installed otelcol-contrib {OTELCOL_CONTRIB_VERSION} ({tag})")
    else:
        print(f"  · otel-collector: otelcol-contrib {OTELCOL_CONTRIB_VERSION} present")
    config = dest_dir / "config.yaml"
    config.write_text(generate_config(repo, ava_home, roles), encoding="utf-8")
    # On a gateway unit the rendered config carries the cluster secret (the
    # pg/redis receiver credentials), same as the cluster .env beside it.
    if platform.system() != "Windows":
        config.chmod(0o600)


def ensure_otel_collector_step(ctx: ConvergeCtx) -> None:
    """Converge step: install the pinned sidecar binary + config (every machine)."""
    ensure_otel_collector(ctx.repo, ctx.ava_home, ctx.roles)
