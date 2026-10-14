"""The runner-class login of a throwaway database (the suite's group-owned test logins).

The Postgres/Redis helpers that used to live here are `base/deploy/tests/containers.py`.
"""

import psycopg

_RUNNER_LOGIN = "ava_g0_runner"
_RUNNER_PASSWORD = "suite-runner-password"  # noqa: S105 — throwaway pg only


def runner_projection(db_url: str | None = None) -> str:
    """`db_url` (default: the suite's owner URL) as the runner-class login the
    agent launcher injects into every agent-profile child.

    Such a child holds no owner password (dotenv_boot drops it), so an owner
    URL cannot configure it. The login is a write generation's runner login
    shape (`grant_runner_login` on the suite database, dialled directly), then
    carried onto `db_url`, which may be a pooler URL.
    """
    from base.config import settings
    from base.host.net.url_secret import url_with_userinfo

    admin = settings.data_plane.db_url
    grant_runner_login(admin, owner="ava_citest", login=_RUNNER_LOGIN, password=_RUNNER_PASSWORD)
    return url_with_userinfo(db_url or admin, _RUNNER_LOGIN, _RUNNER_PASSWORD)


def grant_runner_login(url: str, *, owner: str, login: str, password: str) -> str:
    """Give a throwaway database the runner capability and return a runner URL.

    The production shape: `base.cluster.authority.ensure_groups` converges the
    NOLOGIN `ava_gateway` / `ava_runner` groups on `url`'s database (default
    privileges declared FOR `owner`, the role that creates later tables), and
    `login` inherits `ava_runner` exactly as a write generation's runner login
    does (INHERIT TRUE, SET FALSE, ADMIN FALSE). Idempotent. `url` must dial a
    superuser session.
    """
    from urllib.parse import urlsplit

    from psycopg import sql

    from base.cluster.authority import ensure_groups
    from base.host.db_authority.model import GATEWAY_GROUP, RUNNER_GROUP, Groups
    from base.host.net.url_secret import url_with_userinfo

    database = urlsplit(url).path.strip("/")
    groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
    with psycopg.connect(url, autocommit=True) as conn:
        ensure_groups(conn, owner=owner, database=database, groups=groups)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone() is None:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(login), sql.Literal(password)
                )
            )
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
                    sql.Identifier(RUNNER_GROUP), sql.Identifier(login)
                )
            )
    return url_with_userinfo(url, login, password)
