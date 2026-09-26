"""Database write-generation authority: groups, ledger, logins, fence, invariant.

Application processes never hold schema-owner or administrator credentials.
Two stable NOLOGIN groups carry every application privilege; each write
generation is one gateway and one runner login that inherits its group, owns
nothing and is recorded in the home's private ledger. A rollout revokes the
previous generation, proves its sessions closed, and mints the next one.

Every catalog function takes the caller's admin connection (the OS-user
superuser over the owner-only socket); this package never opens a connection.
Ledger mutations take a typed authority token. ``delivery`` hands the active
generation to the pooler, the launcher and admitted operator processes.
"""

from __future__ import annotations

from shared.cluster.authority.delivery import (
    GENERATION_ENV as GENERATION_ENV,
)
from shared.cluster.authority.delivery import (
    POOLER_ADMIN as POOLER_ADMIN,
)
from shared.cluster.authority.delivery import (
    PoolerAdmin as PoolerAdmin,
)
from shared.cluster.authority.delivery import (
    WriteGrant as WriteGrant,
)
from shared.cluster.authority.delivery import (
    active_generation as active_generation,
)
from shared.cluster.authority.delivery import (
    consume as consume,
)
from shared.cluster.authority.delivery import (
    ensure_pooler_admin as ensure_pooler_admin,
)
from shared.cluster.authority.delivery import (
    read_pooler_admin as read_pooler_admin,
)
from shared.cluster.authority.delivery import (
    render_userlist as render_userlist,
)
from shared.cluster.authority.delivery import (
    write_grant as write_grant,
)
from shared.cluster.authority.fence import (
    close_revoked as close_revoked,
)
from shared.cluster.authority.fence import (
    prove_closure as prove_closure,
)
from shared.cluster.authority.fence import (
    revoke as revoke,
)
from shared.cluster.authority.groups import (
    VacuumSkippedError as VacuumSkippedError,
)
from shared.cluster.authority.groups import (
    apply_group_grants as apply_group_grants,
)
from shared.cluster.authority.groups import (
    ensure_groups as ensure_groups,
)
from shared.cluster.authority.groups import (
    retire_legacy_logins as retire_legacy_logins,
)
from shared.cluster.authority.groups import (
    vacuum_or_fail as vacuum_or_fail,
)
from shared.cluster.authority.invariant import (
    check_invariant as check_invariant,
)
from shared.cluster.authority.ledger import (
    activate as activate,
)
from shared.cluster.authority.ledger import (
    authority_dir as authority_dir,
)
from shared.cluster.authority.ledger import (
    create_ledger as create_ledger,
)
from shared.cluster.authority.ledger import (
    load_ledger as load_ledger,
)
from shared.cluster.authority.ledger import (
    read_secret as read_secret,
)
from shared.cluster.authority.ledger import (
    require_ledger as require_ledger,
)
from shared.cluster.authority.model import (
    GATEWAY_GROUP as GATEWAY_GROUP,
)
from shared.cluster.authority.model import (
    RUNNER_GROUP as RUNNER_GROUP,
)
from shared.cluster.authority.model import (
    AuthorityRefusedError as AuthorityRefusedError,
)
from shared.cluster.authority.model import (
    BirthAuthority as BirthAuthority,
)
from shared.cluster.authority.model import (
    CatalogRefusedError as CatalogRefusedError,
)
from shared.cluster.authority.model import (
    ClosureEvidence as ClosureEvidence,
)
from shared.cluster.authority.model import (
    ClosureRefusedError as ClosureRefusedError,
)
from shared.cluster.authority.model import (
    CutoverAuthority as CutoverAuthority,
)
from shared.cluster.authority.model import (
    Generation as Generation,
)
from shared.cluster.authority.model import (
    GenerationSecret as GenerationSecret,
)
from shared.cluster.authority.model import (
    Groups as Groups,
)
from shared.cluster.authority.model import (
    Ledger as Ledger,
)
from shared.cluster.authority.model import (
    LedgerRefusedError as LedgerRefusedError,
)
from shared.cluster.authority.model import (
    OperationAuthority as OperationAuthority,
)
from shared.cluster.authority.model import (
    VerifiedGeneration as VerifiedGeneration,
)
from shared.cluster.authority.roles import (
    PruneResult as PruneResult,
)
from shared.cluster.authority.roles import (
    SweepResult as SweepResult,
)
from shared.cluster.authority.roles import (
    fenced_roles as fenced_roles,
)
from shared.cluster.authority.roles import (
    mint_generation as mint_generation,
)
from shared.cluster.authority.roles import (
    prune as prune,
)
from shared.cluster.authority.roles import (
    scram_verifier as scram_verifier,
)
from shared.cluster.authority.roles import (
    sweep as sweep,
)
from shared.cluster.authority.roles import (
    verify_generation as verify_generation,
)
