"""Cluster identity (path-only) + the host-level cluster registry.

A cluster is one logical deployment: its OWN Postgres+Redis instance (under its
`$AVA_HOME`, on per-cluster ports), one outward gateway, one host-port
assignment. Two clusters co-located on a box share no data plane — isolation is
home-directory isolation, not an identifier kept correct inside one shared
instance.

**Identity IS the home path.** There is no cluster name: a unit's identity is
the `$AVA_HOME` it runs from (single-machine self-reference), and a remote
runner's identity is the gateway URL + cluster secret it enrolled with
(cross-machine reference). The human-facing label is the home's basename,
computed on the fly — pure display, zero stored state. Identity is born by
`scripts/install.sh` (`python -m cli.install_cluster`); `ava start` is a pure
bring-up; agent-runners inherit connection facts via enroll.

**Names-as-data.** The Postgres database/role and the redis ACL user a cluster
uses are carried by its `.env` connection URLs and read from there as data
(`identity_from_url`), never re-derived from any name — so an existing cluster
whose data plane still uses a historical identifier (prod's `ava_main`) keeps
working unchanged until an explicit ops rename rewrites its URLs. A newly-born
cluster gets the fixed identifier `DATA_PLANE_IDENTITY` (`ava`): its instance
is single-tenant, so the identifier needs no per-cluster distinction.
The package split (2026-08, Task #1007) keeps this module's import surface
byte-for-byte: `shared.cluster` remains the one import target — every public
name plus the test-imported privates (`_swap_db`, `_port_free`) are
re-exported here from the cohesive submodules `registry` (the `clusters.json`
record store), `ports` (port-block allocation + per-record derives), `derive`
(identity/label/session/env derivation), and `provision` (Postgres role/db +
redis ACL ensure). Cross-module calls resolve through this package namespace
at call time, so `monkeypatch.setattr(shared.cluster, ...)` keeps its
single-module semantics.
"""

from __future__ import annotations

from shared.cluster.derive import (
    DATA_PLANE_IDENTITY as DATA_PLANE_IDENTITY,
)
from shared.cluster.derive import (
    REDIS_PASSWORD_ENV as REDIS_PASSWORD_ENV,
)
from shared.cluster.derive import (
    RUNNER_DB_PASSWORD_ENV as RUNNER_DB_PASSWORD_ENV,
)
from shared.cluster.derive import (
    RUNNER_ROLE as RUNNER_ROLE,
)
from shared.cluster.derive import (
    WAKE_KEY_TTL_S as WAKE_KEY_TTL_S,
)
from shared.cluster.derive import (
    db_identity as db_identity,
)
from shared.cluster.derive import (
    default_home as default_home,
)
from shared.cluster.derive import (
    derive_env as derive_env,
)
from shared.cluster.derive import (
    fe_build_env as fe_build_env,
)
from shared.cluster.derive import (
    home_label as home_label,
)
from shared.cluster.derive import (
    home_slug as home_slug,
)
from shared.cluster.derive import (
    identity_from_url as identity_from_url,
)
from shared.cluster.derive import (
    inbound_channel as inbound_channel,
)
from shared.cluster.derive import (
    is_default_home as is_default_home,
)
from shared.cluster.derive import (
    per_cluster_base_urls as per_cluster_base_urls,
)
from shared.cluster.derive import (
    redis_admin_url as redis_admin_url,
)
from shared.cluster.derive import (
    redis_channel_prefix as redis_channel_prefix,
)
from shared.cluster.derive import (
    redis_identity as redis_identity,
)
from shared.cluster.derive import (
    redis_password_from_env as redis_password_from_env,
)
from shared.cluster.derive import (
    runner_password_from_env as runner_password_from_env,
)
from shared.cluster.derive import (
    session_name as session_name,
)
from shared.cluster.derive import (
    slug_for_home as slug_for_home,
)
from shared.cluster.derive import (
    wake_key as wake_key,
)
from shared.cluster.ports import (
    _LATE_HEALTH_SLOTS as _LATE_HEALTH_SLOTS,
)
from shared.cluster.ports import (
    ClusterPorts as ClusterPorts,
)
from shared.cluster.ports import (
    _port_free as _port_free,
)
from shared.cluster.ports import (
    allocate_ports as allocate_ports,
)
from shared.cluster.ports import (
    record_app_port as record_app_port,
)
from shared.cluster.ports import (
    record_health_port as record_health_port,
)
from shared.cluster.ports import (
    record_pgbouncer_port as record_pgbouncer_port,
)
from shared.cluster.ports import (
    record_postgres_port as record_postgres_port,
)
from shared.cluster.ports import (
    record_redis_port as record_redis_port,
)
from shared.cluster.provision import (
    _adopt_database as _adopt_database,
)
from shared.cluster.provision import (
    _schema_applied as _schema_applied,
)
from shared.cluster.provision import (
    _swap_db as _swap_db,
)
from shared.cluster.provision import (
    assert_checkpoint_dependency_pinned as assert_checkpoint_dependency_pinned,
)
from shared.cluster.provision import (
    assert_checkpoint_schema_current as assert_checkpoint_schema_current,
)
from shared.cluster.provision import (
    drop_database as drop_database,
)
from shared.cluster.provision import (
    ensure_checkpoint_schema as ensure_checkpoint_schema,
)
from shared.cluster.provision import (
    ensure_cluster_redis_acl as ensure_cluster_redis_acl,
)
from shared.cluster.provision import (
    ensure_cluster_role as ensure_cluster_role,
)
from shared.cluster.provision import (
    ensure_runner_role as ensure_runner_role,
)
from shared.cluster.provision import (
    provision_database as provision_database,
)
from shared.cluster.registry import (
    ClusterRecord as ClusterRecord,
)
from shared.cluster.registry import (
    _dump_registry as _dump_registry,
)
from shared.cluster.registry import (
    _registry_disk_form as _registry_disk_form,
)
from shared.cluster.registry import (
    delete_record as delete_record,
)
from shared.cluster.registry import (
    delete_record_locked as delete_record_locked,
)
from shared.cluster.registry import (
    get_record as get_record,
)
from shared.cluster.registry import (
    load_registry as load_registry,
)
from shared.cluster.registry import (
    migrate_registry_keys as migrate_registry_keys,
)
from shared.cluster.registry import (
    registry_lock as registry_lock,
)
from shared.cluster.registry import (
    registry_path as registry_path,
)
from shared.cluster.registry import (
    save_record as save_record,
)
from shared.cluster.registry import (
    save_record_locked as save_record_locked,
)
from shared.config import settings as settings
from shared.platform import file_lock as file_lock
from shared.port_block import (
    BLOCK_MAX as BLOCK_MAX,
)
from shared.port_block import (
    BLOCK_SIZE as BLOCK_SIZE,
)
from shared.port_block import (
    BLOCK_START as BLOCK_START,
)
from shared.port_block import (
    LEGACY_AVA_PORTS as LEGACY_AVA_PORTS,
)
from shared.port_block import (
    PORT_OFFSETS as PORT_OFFSETS,
)
from shared.url_secret import url_with_port as url_with_port
from shared.url_secret import url_with_userinfo as url_with_userinfo
