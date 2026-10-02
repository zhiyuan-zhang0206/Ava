"""Cluster identity and membership: path-only identity and each home's own record,
this host's machine identity and the machines roster, cluster auth,
ports, and the per-cluster data-plane instances.

A cluster is one logical deployment: its OWN Postgres+Redis instance (under its
`$AVA_HOME`, on the fixed port table), one outward gateway. A host runs one
cluster; its ports are the fixed table every new home records at birth.

**Identity IS the home path.** There is no cluster name: a unit's identity is
the `$AVA_HOME` it runs from (single-machine self-reference), and a remote
runner's identity is the gateway URL + cluster secret it joins with.
The human-facing label is the home's basename. The single `ava start`
lifecycle persists identity before effects and brings up or resumes that home;
runner initialization obtains its explicit gateway connection projection.

**Names-as-data.** The Postgres database/role and the redis ACL user a cluster
uses are carried by its `.env` connection URLs and read from there as data
(`identity_from_url`), never re-derived from any name — so an existing cluster
whose data plane still uses a historical identifier (prod's `ava_main`) keeps
working unchanged until an explicit ops rename rewrites its URLs. A newly-born
cluster gets the fixed identifier `DATA_PLANE_IDENTITY` (`ava`): its instance
is single-tenant, so the identifier needs no per-cluster distinction.
The public namespace exports the home record, the fixed port table, URL
derivation, Postgres provisioning and Redis ACL helpers. `ownership` provides the shared
native storage observer used before startup and maintenance effects, and the home's Redis
data directory and configured port it observes.

Membership and access live in member modules the door does not re-export:
`machine` (this host's machine name and capability set), `machines` and
`machine_exclusions` (the multi-machine roster and its operator exclusions),
`auth` (cluster bearer auth), `rate_limit` (gateway login throttling),
`transport_encryption` (the precondition for secret-bearing off-box listeners)
and `port_preflight` (the listener scan the ownership checks and health checks share, and
the bind posture, `bind_addrs`, the data plane's listeners are expected to hold).
"""

from __future__ import annotations

from base.cluster.derive import (
    DATA_PLANE_IDENTITY as DATA_PLANE_IDENTITY,
)
from base.cluster.derive import (
    REDIS_PASSWORD_ENV as REDIS_PASSWORD_ENV,
)
from base.cluster.derive import (
    RUNNER_DB_PASSWORD_ENV as RUNNER_DB_PASSWORD_ENV,
)
from base.cluster.derive import (
    RUNNER_ROLE as RUNNER_ROLE,
)
from base.cluster.derive import (
    WAKE_KEY_TTL_S as WAKE_KEY_TTL_S,
)
from base.cluster.derive import (
    db_identity as db_identity,
)
from base.cluster.derive import (
    default_home as default_home,
)
from base.cluster.derive import (
    derive_env as derive_env,
)
from base.cluster.derive import (
    fe_build_env as fe_build_env,
)
from base.cluster.derive import (
    frontend_service_cmd as frontend_service_cmd,
)
from base.cluster.derive import (
    home_label as home_label,
)
from base.cluster.derive import (
    home_slug as home_slug,
)
from base.cluster.derive import (
    identity_from_url as identity_from_url,
)
from base.cluster.derive import (
    inbound_channel as inbound_channel,
)
from base.cluster.derive import (
    is_default_home as is_default_home,
)
from base.cluster.derive import (
    per_cluster_base_urls as per_cluster_base_urls,
)
from base.cluster.derive import (
    redis_admin_url as redis_admin_url,
)
from base.cluster.derive import (
    redis_channel_prefix as redis_channel_prefix,
)
from base.cluster.derive import (
    redis_identity as redis_identity,
)
from base.cluster.derive import (
    redis_password_from_env as redis_password_from_env,
)
from base.cluster.derive import (
    session_name as session_name,
)
from base.cluster.derive import (
    wake_key as wake_key,
)
from base.cluster.ports import (
    ClusterPorts as ClusterPorts,
)
from base.cluster.ports import (
    new_home_ports as new_home_ports,
)
from base.cluster.ports import (
    port_free as port_free,
)
from base.cluster.provision import (
    _adopt_database as _adopt_database,
)
from base.cluster.provision import (
    _schema_applied as _schema_applied,
)
from base.cluster.provision import (
    _swap_db as _swap_db,
)
from base.cluster.provision import (
    assert_checkpoint_dependency_pinned as assert_checkpoint_dependency_pinned,
)
from base.cluster.provision import (
    assert_checkpoint_schema_current as assert_checkpoint_schema_current,
)
from base.cluster.provision import (
    drop_database as drop_database,
)
from base.cluster.provision import (
    ensure_checkpoint_schema as ensure_checkpoint_schema,
)
from base.cluster.provision import (
    ensure_cluster_role as ensure_cluster_role,
)
from base.cluster.provision import (
    ensure_pgvector_extension as ensure_pgvector_extension,
)
from base.cluster.provision import (
    provision_database as provision_database,
)
from base.cluster.record import (
    INTENT_NAME as INTENT_NAME,
)
from base.cluster.record import (
    ClusterRecord as ClusterRecord,
)
from base.cluster.record import (
    get_record as get_record,
)
from base.cluster.redis_acl import (
    ensure_cluster_redis_acl as ensure_cluster_redis_acl,
)
from base.host.env.port_table import (
    FIXED_PORTS as FIXED_PORTS,
)
from base.host.net.url_secret import url_with_port as url_with_port
from base.host.net.url_secret import url_with_userinfo as url_with_userinfo
from base.native_process.os_platform import file_lock as file_lock
