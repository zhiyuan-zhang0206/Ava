"""Database write-generation authority: groups, ledger, logins, invariant.

Application processes never hold schema-owner or administrator credentials.
Two stable NOLOGIN groups carry every application privilege; each write
generation is one gateway and one runner login that inherits its group, owns
nothing and is recorded in the home's private ledger. A home has exactly one
generation, minted by its first start.

Every catalog function takes the caller's admin connection (the OS-user
superuser over the owner-only socket); this package never opens a connection.
``delivery`` hands the active generation to the pooler, the launcher and
admitted operator processes.
``monitor`` keeps the one stable read-only login outside the generations: the
collector's password-less, peer-authenticated statistics reader.
"""

from __future__ import annotations

from base.cluster.authority.delivery import (
    GENERATION_ENV as GENERATION_ENV,
)
from base.cluster.authority.delivery import (
    POOLER_ADMIN as POOLER_ADMIN,
)
from base.cluster.authority.delivery import (
    PoolerAdmin as PoolerAdmin,
)
from base.cluster.authority.delivery import (
    WriteGrant as WriteGrant,
)
from base.cluster.authority.delivery import (
    active_generation as active_generation,
)
from base.cluster.authority.delivery import (
    consume as consume,
)
from base.cluster.authority.delivery import (
    ensure_pooler_admin as ensure_pooler_admin,
)
from base.cluster.authority.delivery import (
    operator_environment as operator_environment,
)
from base.cluster.authority.delivery import (
    read_pooler_admin as read_pooler_admin,
)
from base.cluster.authority.delivery import (
    render_userlist as render_userlist,
)
from base.cluster.authority.delivery import (
    write_grant as write_grant,
)
from base.cluster.authority.groups import (
    VacuumSkippedError as VacuumSkippedError,
)
from base.cluster.authority.groups import (
    apply_group_grants as apply_group_grants,
)
from base.cluster.authority.groups import (
    ensure_groups as ensure_groups,
)
from base.cluster.authority.groups import (
    retire_legacy_logins as retire_legacy_logins,
)
from base.cluster.authority.groups import (
    vacuum_or_fail as vacuum_or_fail,
)
from base.cluster.authority.invariant import (
    check_invariant as check_invariant,
)
from base.cluster.authority.ledger import (
    activate as activate,
)
from base.cluster.authority.ledger import (
    authority_dir as authority_dir,
)
from base.cluster.authority.ledger import (
    create_ledger as create_ledger,
)
from base.cluster.authority.ledger import (
    load_ledger as load_ledger,
)
from base.cluster.authority.ledger import (
    read_secret as read_secret,
)
from base.cluster.authority.ledger import (
    require_ledger as require_ledger,
)
from base.cluster.authority.model import (
    GATEWAY_GROUP as GATEWAY_GROUP,
)
from base.cluster.authority.model import (
    RUNNER_GROUP as RUNNER_GROUP,
)
from base.cluster.authority.model import (
    AuthorityRefusedError as AuthorityRefusedError,
)
from base.cluster.authority.model import (
    CatalogRefusedError as CatalogRefusedError,
)
from base.cluster.authority.model import (
    Generation as Generation,
)
from base.cluster.authority.model import (
    GenerationSecret as GenerationSecret,
)
from base.cluster.authority.model import (
    Groups as Groups,
)
from base.cluster.authority.model import (
    Ledger as Ledger,
)
from base.cluster.authority.model import (
    LedgerRefusedError as LedgerRefusedError,
)
from base.cluster.authority.model import (
    VerifiedGeneration as VerifiedGeneration,
)
from base.cluster.authority.monitor import (
    MONITOR_MAP as MONITOR_MAP,
)
from base.cluster.authority.monitor import (
    MONITOR_ROLE as MONITOR_ROLE,
)
from base.cluster.authority.monitor import (
    ensure_monitor as ensure_monitor,
)
from base.cluster.authority.roles import (
    mint_generation as mint_generation,
)
from base.cluster.authority.roles import (
    scram_verifier as scram_verifier,
)
from base.cluster.authority.roles import (
    verify_generation as verify_generation,
)
