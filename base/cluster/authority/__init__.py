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
    vacuum_or_fail as vacuum_or_fail,
)
from base.cluster.authority.invariant import (
    check_invariant as check_invariant,
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
