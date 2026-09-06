"""OTel Collector sidecar install — pinned binary download + config generation.

One collector per machine (task #1266): every agent on the host sends OTLP to
its local receiver (127.0.0.1, on AVA_TELEMETRY_OTLP_PORT — default the
standard 4318). A gateway collector fans out to its loopback
Tempo/Loki/Prometheus backends; a pure runner collector relays each signal to
the gateway collector's authenticated private-address receiver. Every machine
mirrors its local traces to JSONL. This module is the converge step's engine —
idempotent: an already-installed, version-matching binary is a no-op; the
config is regenerated on every converge so fan-out/retention setting changes
propagate on the next `ava start`.

The CONTRIB distribution is required, not core: the file exporter (JSONL
mirror) and the file_storage extension (persistent sending queue) live in
contrib. The version is pinned to the collector release the LGTM stack
(deploy/lgtm) was validated with, and each platform's release tarball is
SHA256-pinned.

Remote OTLP ingress is the bearer-authenticated machine-to-machine surface
(conventions/reachability-and-credentials.md): a gateway sidecar accepts
pure-runner relays, and an observability-station sidecar accepts remote
gateway collectors — both through `otlp/remote` + `bearertokenauth/cluster`
with the cluster secret.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from string import Template
from urllib.parse import unquote, urlsplit

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._otel_collector_exporters import BACKEND_EXPORTERS, RELAY_EXPORTERS
from cli.commands._rendered_file import write_rendered_guarded
from shared.machine import MachineRoles
from shared.observability import collector_allowed_for_home

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

# Download fetch discipline (issue #172): the tarball is on the critical path
# of `ava start`, so a slow or dead mirror must fail the converge step in
# bounded time and say so — never stall bring-up indefinitely. Per-read socket
# timeout catches a wedged connection; a total wall-clock cap per attempt
# catches a mirror that trickles forever; a bounded retry rides out transient
# blips; a heartbeat line distinguishes slow from dead.
_DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
_DOWNLOAD_ATTEMPT_TIMEOUT_S = 600.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5.0
_DOWNLOAD_PROGRESS_INTERVAL_S = 15.0


def _otlp_ingress_port() -> int:
    """The OTLP/HTTP ingress port — the single settings source (WP3, task #1945).

    Rendered into the sidecar receiver, the gateway's authenticated remote
    receiver, and the pure-runner relay endpoint; the roster gate and the
    sidecar healthcheck probe the same port. Never a second literal here.
    """
    from shared.config import settings

    return settings.observability.telemetry_otlp_port


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


def _ottl_quote(value: str) -> str:
    """A JSON string literal, which is also an escaped OTTL string literal."""
    return json.dumps(value, ensure_ascii=False)


def _unspecified_address(host: str) -> bool:
    """True for wildcard IP literals (0.0.0.0 / ::), never hostnames."""
    candidate = host.strip().removeprefix("[").removesuffix("]")
    try:
        return ipaddress.ip_address(candidate).is_unspecified
    except ValueError:
        return False


def _host_port(host: str, port: int) -> str:
    """Render host:port without making IPv6 literals ambiguous."""
    bare = host.strip().removeprefix("[").removesuffix("]")
    rendered = f"[{bare}]" if ":" in bare else bare
    return f"{rendered}:{port}"


# Data-plane receiver fragments rendered into $DATA_PLANE_RECEIVERS. Scrape
# interval is 60s (not host_metrics' 30s): connection counts and redis memory
# are slow-moving pressure gauges.
_POSTGRES_RECEIVER_BLOCK = """
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
"""

_REDIS_RECEIVER_BLOCK = """
  # This cluster's OWN Redis. The receiver dials the default administrative
  # user, so it uses the gateway-only Redis admin password; an empty password
  # is the no-auth single-box default and the receiver skips AUTH.
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
    from shared.cluster import redis_admin_url

    redis_admin = redis_admin_url()
    blocks: list[str] = []
    receivers: list[str] = []
    if pg.password and settings.observability.telemetry_otlp_enabled:
        blocks.append(
            _POSTGRES_RECEIVER_BLOCK.format(
                pg_endpoint=_endpoint(db_url, "postgres"),
                pg_user=_yaml_quote(unquote(pg.username or "")),
                pg_password=_yaml_quote(unquote(pg.password)),
                pg_database=_yaml_quote(pg.path.lstrip("/")),
            )
        )
        receivers.append("postgresql")
    # The contrib postgresql receiver rejects an empty password, so no-auth
    # single-box homes omit only that receiver. Redis supports an empty
    # password and remains observable in the same posture.
    blocks.append(
        _REDIS_RECEIVER_BLOCK.format(
            redis_endpoint=_endpoint(redis_url, "redis"),
            redis_password=_yaml_quote(unquote(urlsplit(redis_admin).password or "")),
        )
    )
    receivers.append("redis")
    return "".join(blocks), "".join(f", {receiver}" for receiver in receivers)


def gateway_otel_ingress_endpoint() -> str:
    """The authenticated OTLP/HTTP ingress a pure runner dials.

    `AVA_GATEWAY_URL` is already the runner's cluster-private route to the
    gateway. OTLP owns a separate fixed port on that same host; it does not
    inherit the gateway API's port or path. The receiver is deliberately HTTP:
    the cluster private network supplies transport privacy,
    while the cluster bearer authenticates the sender exactly like the gateway
    API already does.
    """
    from shared.config import settings
    from shared.netutil import is_loopback_host

    gateway_url = settings.gateway.gateway_url.strip()
    parts = urlsplit(gateway_url)
    host = parts.hostname or ""
    if not host or is_loopback_host(host) or _unspecified_address(host):
        raise RuntimeError(
            "cannot build runner OTLP relay: gateway URL (AVA_GATEWAY_URL) must name the "
            f"gateway's non-loopback private address (got {gateway_url!r})"
        )
    return f"http://{_host_port(host, _otlp_ingress_port())}"


def station_otel_ingress_endpoint() -> str:
    """The remote observatory station's authenticated OTLP/HTTP ingress.

    `http://<AVA_OBSERVABILITY_URL host>:<OTLP port>` — the one station
    address a remote gateway collector relays to (WP4, task #1946). The
    station side authenticates the sender with the cluster bearer
    (bearertokenauth/cluster on its `otlp/remote` receiver, rendered by
    `_remote_receiver_fragments`); the port follows
    AVA_TELEMETRY_OTLP_PORT (single source, task #1945), so the relay target
    and the station's advertised unit url (shared.machines.unit_dial_url)
    can never drift apart.
    """
    from cli.commands._observatory_urls import _validated_observability_base
    from shared.config import settings

    base = _validated_observability_base(settings.observability.observability_url)
    if not base:
        raise RuntimeError(
            "cannot build the remote-station OTLP relay without a valid "
            "AVA_OBSERVABILITY_URL (scheme://host, no port, no path)"
        )
    return f"{base}:{_otlp_ingress_port()}"


def _cluster_bearer() -> str:
    from shared.config import settings

    secret = settings.data_plane.cluster_secret
    if not secret:
        raise RuntimeError(
            "cannot build split-cluster OTLP relay without a cluster secret "
            "(AVA_CLUSTER_SECRET); "
            "remote ingress must fail closed"
        )
    return f"Bearer {secret}"


def _remote_receiver_fragments(roles: MachineRoles | None) -> dict[str, str]:
    """Template fragments for the authenticated remote OTLP ingress.

    Served by any unit that remote peers dial OTLP into: a gateway (pure
    runner relays) and an observability station (remote gateway collectors,
    WP4, task #1946 — conventions/reachability-and-credentials.md). Remote
    ingress exists only when the unit could actually have remote peers: an
    empty cluster secret (the zero-config single-box posture) and a loopback
    reachable host (co-located posture) both mean NO remote peers, so no
    receiver is rendered — the same "legal when nothing remote dials it" rule
    as the registration loopback guard (shared.machines._reject_loopback_dial_url,
    conventions rule 2). A wildcard reachable host is a configuration error
    either way and fails closed. Any gateway- or station-capable host with a
    non-empty secret and a non-loopback address may serve remote peers —
    including a hybrid gateway+runner+station such as production. Remote
    traces use a separate pipeline so they cannot be mirrored a second time.
    """
    no_remote = {
        "REMOTE_OTLP_RECEIVER": "",
        "CLUSTER_AUTH_EXTENSION": "",
        "CLUSTER_AUTH_SERVICE_EXTENSION": "",
        "REMOTE_OTLP_PIPELINE_RECEIVER": "",
        "REMOTE_TRACE_PIPELINE": "",
    }
    if roles is None or not (roles & {"gateway", "observability-station"}):
        return no_remote

    from shared.config import settings
    from shared.machine import reachable_host
    from shared.netutil import is_loopback_host

    secret = settings.data_plane.cluster_secret
    host = reachable_host()
    if _unspecified_address(host):
        # 0.0.0.0 / :: is never a reachable host (the loopback classifier
        # treats 0.0.0.0 as loopback, so check the wildcard form first): a
        # unit with a wildcard identity cannot serve remote peers at a
        # meaningful address — fail closed, whichever roles it carries.
        raise RuntimeError(
            "cannot expose authenticated OTLP ingress: reachable host is "
            "wildcard; set AVA_MACHINE_HOST to this host's exact "
            "private address"
        )
    if not secret or is_loopback_host(host):
        # Empty secret or loopback host = no remote peers exist (single-box
        # posture). No receiver, no error — same rule the registration guard
        # applies (a co-located unit may advertise loopback).
        return no_remote
    return {
        "REMOTE_OTLP_RECEIVER": f"""
  otlp/remote:
    protocols:
      http:
        endpoint: {_yaml_quote(_host_port(host, _otlp_ingress_port()))}
        auth:
          authenticator: bearertokenauth/cluster
""",
        "CLUSTER_AUTH_EXTENSION": f"""
  bearertokenauth/cluster:
    token: {_yaml_quote(secret)}
""",
        "CLUSTER_AUTH_SERVICE_EXTENSION": ", bearertokenauth/cluster",
        "REMOTE_OTLP_PIPELINE_RECEIVER": ", otlp/remote",
        "REMOTE_TRACE_PIPELINE": """
    # A remote peer already writes its own durable trace mirror. The remote
    # pipeline therefore fans out only to Tempo; adding file/traces here
    # duplicates every remote span in the local mirror and makes replay
    # provenance ambiguous.
    traces/remote:
      receivers: [otlp/remote]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/tempo]
""",
    }


def _otlp_exporters(roles: MachineRoles | None) -> str:
    """Role-specific fan-out with stable component/queue identities.

    Three shapes (conventions/reachability-and-credentials.md):
    - a pure agent-runner relays every signal to its gateway's authenticated
      OTLP ingress (`otlp/remote` on the gateway sidecar);
    - a unit that consumes a remote observatory (`AVA_OBSERVABILITY_URL` set)
      relays every signal to the station's single bearer-authenticated OTLP
      ingress — never dials the station's loopback-bound native backends
      directly;
    - everything else fans out to its local backends (loopback by default,
      or the per-service settings URLs).
    """
    from shared.config import settings

    if roles == frozenset({"agent-runner"}):
        return RELAY_EXPORTERS.format(
            endpoint=_yaml_quote(gateway_otel_ingress_endpoint()),
            authorization=_yaml_quote(_cluster_bearer()),
        )
    obs = settings.observability
    if obs.observability_url:
        return RELAY_EXPORTERS.format(
            endpoint=_yaml_quote(station_otel_ingress_endpoint()),
            authorization=_yaml_quote(_cluster_bearer()),
        )
    loki_base, prom_base = _lgtm_fanout_bases()
    return BACKEND_EXPORTERS.format(
        tempo_endpoint=_yaml_quote(obs.telemetry_tempo_endpoint.rstrip("/")),
        loki_base=_yaml_quote(loki_base),
        prom_base=_yaml_quote(prom_base),
    )


def generate_config(repo: Path, ava_home: Path, roles: MachineRoles | None) -> str:
    """Render the sidecar config from the repo template + this unit's settings."""
    from shared.cluster import home_label
    from shared.config import settings
    from shared.machine import machine_name

    obs = settings.observability
    loki_base, prom_base = _lgtm_fanout_bases()
    data_plane_block, data_plane_pipeline = _data_plane_receivers(roles)
    substitutions = {
        "AVA_HOME": str(ava_home),
        "CLUSTER_LABEL": home_label(ava_home),
        "MACHINE_NAME": _ottl_quote(machine_name()),
        # Kept as named generation inputs for small downstream/custom templates;
        # the shipped template consumes the role-specific OTLP_EXPORTERS block.
        "TEMPO_ENDPOINT": obs.telemetry_tempo_endpoint.rstrip("/"),
        "LOKI_BASE": loki_base,
        "PROM_BASE": prom_base,
        "RETENTION_DAYS": str(obs.trace_retention_days),
        "SELF_METRICS_PORT": str(settings.observability.otel_collector_metrics_port),
        "OTLP_RECEIVER_ENDPOINT": _host_port("127.0.0.1", _otlp_ingress_port()),
        "DATA_PLANE_RECEIVERS": data_plane_block,
        "DATA_PLANE_PIPELINE_RECEIVERS": data_plane_pipeline,
        "OTLP_EXPORTERS": _otlp_exporters(roles),
    }
    substitutions.update(_remote_receiver_fragments(roles))
    return Template(_config_template(repo)).substitute(substitutions)


def _stream_download(url: str, dest: Path) -> None:
    """Stream ``url`` into ``dest`` with the issue #172 fetch discipline.

    A bounded, loud download: per-read socket timeout, a wall-clock cap on the
    whole attempt, a progress heartbeat every 15 s (so a slow mirror reads as
    "slow", not "hung"), and a RuntimeError naming the URL + elapsed time when
    the attempt exceeds its budget. Deliberately not ``urlretrieve`` — it has
    no timeout parameter, which is exactly the gap this fixes.
    """
    started = time.monotonic()
    last_beat = started
    got = 0
    total: int | None = None
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_SOCKET_TIMEOUT_S) as resp,  # noqa: S310 — pinned https release asset
        dest.open("wb") as fh,
    ):
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = None
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            now = time.monotonic()
            if now - last_beat >= _DOWNLOAD_PROGRESS_INTERVAL_S:
                pct = f" ({got * 100 // total}%)" if total else ""
                print(
                    f"  · otel-collector: {got / 1e6:.1f} MB{pct} in {now - started:.0f}s",
                    flush=True,
                )
                last_beat = now
            if now - started > _DOWNLOAD_ATTEMPT_TIMEOUT_S:
                raise TimeoutError(
                    f"download exceeded {_DOWNLOAD_ATTEMPT_TIMEOUT_S:.0f}s wall-clock cap"
                )
    print(f"  · otel-collector: downloaded {got / 1e6:.1f} MB in {time.monotonic() - started:.0f}s")


def _download_with_retry(url: str, tarball: Path) -> None:
    """Bounded retry around ``_stream_download``; a final failure names the
    URL and total elapsed time so the operator knows exactly what to fix."""
    started = time.monotonic()
    last_err: Exception | None = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            _stream_download(url, tarball)
            return
        except Exception as exc:  # URLError / TimeoutError / OSError
            last_err = exc
            elapsed = time.monotonic() - started
            print(
                f"  ! otel-collector: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} "
                f"failed after {elapsed:.0f}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if attempt < _DOWNLOAD_ATTEMPTS:
                time.sleep(_DOWNLOAD_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"failed to download otel-collector from {url} after {_DOWNLOAD_ATTEMPTS} "
        f"attempts ({time.monotonic() - started:.0f}s total): {last_err}"
    ) from last_err


def _download_and_verify(tag: str, dest_dir: Path) -> None:
    """Download + SHA256-verify + extract the pinned tarball into dest_dir."""
    url = _DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag)
    expected = _OTELCOL_CONTRIB_SHA256[tag]
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        print(f"  · otel-collector: downloading {url}")
        _download_with_retry(url, tarball)
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


def _lgtm_fanout_bases() -> tuple[str, str]:
    """The gateway collector's LGTM fan-out base URLs (loki, prometheus).

    Two-state on AVA_OBSERVABILITY_URL (task #1791, A3): empty (default) keeps
    the per-service settings URLs, whose defaults are this host's loopback
    backends; non-empty points the fan-out at the remote observatory station
    (base URL + the service's own port). The runner relay path is unaffected —
    it always relays to the gateway collector.
    """
    from cli.commands._observatory_urls import _validated_observability_base
    from shared.config import settings

    obs = settings.observability
    base = _validated_observability_base(obs.observability_url)
    if base:
        # Remote observatory: every signal enters the station through ONE
        # bearer-authenticated OTLP ingress (WP4) — the direct
        # /otlp fan-out to the station's loopback-bound backends is gone.
        return f"{base}:{_otlp_ingress_port()}", f"{base}:{_otlp_ingress_port()}"
    return (
        obs.telemetry_loki_url.rstrip("/") + "/otlp",
        obs.telemetry_prometheus_url.rstrip("/") + "/api/v1/otlp",
    )


def _atomic_write(path: Path, rendered: str) -> None:
    """Replace ``path`` with ``rendered`` atomically (mkstemp + rename)."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_config(path: Path, rendered: str) -> None:
    """Publish the rendered collector config with user-edit protection.

    Content-hash guarded (web-sources precedent, task #1791 A3): a config.yaml
    the user hand-edited since the last converge write is warned about and
    preserved, never overwritten. The write itself stays atomic and owner-only
    from first creation — split runners carry the cluster bearer and gateways
    may additionally carry data-plane credentials, so the file must never be
    exposed through the process umask (``mkstemp`` creates 0600 and
    replacement publishes only the fully-written private file).
    """
    hashes_path = path.parent / "rendered-hashes.json"
    warning = write_rendered_guarded(
        path,
        rendered,
        hashes_path,
        path.name,
        writer=_atomic_write,
    )
    if warning is not None:
        print(f"  ! otel-collector: {warning}", file=sys.stderr)


def ensure_otel_collector(repo: Path, ava_home: Path, roles: MachineRoles | None) -> None:
    """Idempotent install: pinned binary present + config regenerated.

    Binary download is skipped when the version marker matches. Config is
    re-rendered on every converge (template changes and setting changes
    propagate on the next converge) — except a config.yaml the user hand-edited
    since the last converge write, which is warned about and preserved
    (content-hash guard, web-sources precedent). Unsupported platforms warn
    and skip — the sidecar session will not start and the agent preflight
    disables OTLP export with a reported warning.
    """
    from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_otel_binary

    if WHEEL_RUNTIME:
        binary = runtime_otel_binary()
        marker = binary.parent / _VERSION_MARKER
        if not binary.is_file() or marker.read_text().strip() != OTELCOL_CONTRIB_VERSION:
            raise RuntimeError("verified release lacks its pinned collector; prepare before start")
        dest_dir = ava_home / "otel-collector"
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write_config(dest_dir / "config.yaml", generate_config(repo, ava_home, roles))
        return
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
    _write_config(dest_dir / "config.yaml", generate_config(repo, ava_home, roles))


def _reap_orphan_collector_session() -> None:
    """Stop a collector that predates this gateway losing its LGTM marker.

    The roster gate drops the collector from a non-LGTM gateway, so the
    watchdog will not revive it, but a session started before the marker went
    away keeps running until something stops it. Converge is the reconcile
    point, so ``ava start`` and ``ava cluster update`` reach the gated roster.
    Its force-kill fallback is the designed remedy for this operator-initiated
    transition, so the backend logs an escalation at INFO instead of WARNING.
    """
    import cli.commands as _ns
    from cli.commands._session_lifecycle import _graceful_kill_session
    from shared.cluster import session_name

    sess = session_name("otel-collector")
    if not _ns._has_session(sess):
        return
    ok, mode = _graceful_kill_session(sess, expected=True)
    print(
        f"  ! otel-collector: reaped orphan session {sess} "
        f"({'✓' if ok else '✗'} {mode}) — gateway no longer owns a collector",
        file=sys.stderr,
    )


def ensure_otel_collector_step(ctx: ConvergeCtx) -> None:
    """Install a pure-runner relay or the designated LGTM host's collector.

    A gateway home is skipped only when it is neither the LGTM host nor opted
    into an explicit ``AVA_TELEMETRY_OTLP_ENDPOINT`` override — same rule as
    the roster gate (``ops.spec``) and the sidecar healthcheck.
    """
    if (
        ctx.roles is not None
        and "gateway" in ctx.roles
        and not collector_allowed_for_home(ctx.ava_home)
    ):
        print(
            "  ! otel-collector: collector skipped — this gateway home is not "
            "the observability station ("
            f"{ctx.ava_home / 'lgtm-host'} is absent and the observability-station "
            "capability is not declared); telemetry export is unavailable; set "
            "AVA_TELEMETRY_OTLP_ENDPOINT to use an explicit collector",
            file=sys.stderr,
        )
        residual = ctx.ava_home / "otel-collector/config.yaml"
        if residual.exists():
            print(
                f"  ! otel-collector: stale/residual config remains at {residual}; "
                "collector files were preserved",
                file=sys.stderr,
            )
        _reap_orphan_collector_session()
        return
    ensure_otel_collector(ctx.repo, ctx.ava_home, ctx.roles)
