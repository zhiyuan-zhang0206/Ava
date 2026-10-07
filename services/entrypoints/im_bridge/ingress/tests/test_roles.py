"""Actual generation roles preserve gateway-only durable ingress mutation."""

import psycopg
import pytest
from psycopg import sql

from tests.path_scoped.db_authority_tests import AuthorityCluster


@pytest.mark.parametrize(
    "table", ["weixin_ingress_bindings", "weixin_ingress_cursors", "weixin_ingress_receipts"]
)
def test_provider_receipts_and_cursor_mutations_are_gateway_owned(
    authority_postgres: AuthorityCluster, table: str
) -> None:
    cluster = authority_postgres
    secret = cluster.active_secret()
    for cls, writable in (("gateway", True), ("runner", False)):
        role = secret.roles.of("gateway") if cls == "gateway" else secret.roles.of("runner")
        with cluster.login(role.name, role.password) as conn:
            permissions = conn.execute(
                "SELECT has_table_privilege(current_user,%s,'INSERT'),has_table_privilege(current_user,%s,'UPDATE'),has_table_privilege(current_user,%s,'DELETE')",
                (table, table, table),
            ).fetchone()
            assert permissions == (writable, writable, writable)
            if not writable:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        sql.SQL("DELETE FROM {} WHERE false").format(sql.Identifier(table))
                    )
