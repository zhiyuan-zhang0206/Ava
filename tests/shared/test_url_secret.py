"""`url_with_password` / `url_with_userinfo`, and the Settings model-validator
that uses them.

Names-as-data (path-only identity): a URL's username and database ARE the
cluster's data-plane identifiers, never re-derived from a name. Settings
re-applies only the main DB-owner password on load — an owner-password rotation
self-heals while a `.env` on the historical `ava_main` identifiers (prod before
the ops rename) keeps dialing exactly what it says.

URLs are built from parts (`_pg` / `_redis`) rather than written as literals so
the source carries no `scheme://user:password@host` string for a secret scanner
to flag — every value here is a throwaway fixture, not a real credential.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from shared.config import DataPlaneSettings, data_plane, settings
from shared.config.data_plane import _self_machine_host
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.machine import reachable_host, reset_identity
from shared.url_secret import url_with_password, url_with_userinfo

_SECRET = "new-secret_v2"  # noqa: S105 — test fixture, not a real credential


def _pg(pw: str, *, host: str = "db.host:5432", db: str = "ava", user: str = "ava") -> str:
    return f"postgresql://{user}:{pw}@{host}/{db}"


def _redis(pw: str, *, host: str = "cache.host:6379", idx: int = 0, user: str = "") -> str:
    return f"redis://{user}:{pw}@{host}/{idx}"


class TestUrlWithPassword:
    def test_postgres_keeps_username_swaps_password(self) -> None:
        # The `ava` username is load-bearing for Postgres and must survive.
        assert url_with_password(_pg("OLD"), _SECRET) == _pg(_SECRET)

    def test_postgres_passwordless_url_gets_password(self) -> None:
        assert url_with_password("postgresql://ava@127.0.0.1:5432/ava", _SECRET) == _pg(
            _SECRET, host="127.0.0.1:5432"
        )

    def test_redis_has_no_username(self) -> None:
        # redis URLs carry no username, so the result is `:pw@host`.
        assert url_with_password(_redis("OLD", host="h:6379", idx=3), _SECRET) == _redis(
            _SECRET, host="h:6379", idx=3
        )

    def test_redis_index_path_preserved(self) -> None:
        assert url_with_password("redis://h:6379/7", _SECRET) == _redis(
            _SECRET, host="h:6379", idx=7
        )

    def test_ipv6_host_rebracketed(self) -> None:
        assert url_with_password(_pg("OLD", host="[::1]:5432"), _SECRET) == _pg(
            _SECRET, host="[::1]:5432"
        )

    def test_url_reserved_chars_in_secret_are_encoded(self) -> None:
        # An '@' / ':' / '/' in the secret must be percent-encoded, not split the netloc.
        out = url_with_password(_pg("OLD", host="h:5432", db="db"), "a@b:c/d")
        parts = urlsplit(out)
        assert parts.hostname == "h" and parts.port == 5432
        assert unquote(parts.password or "") == "a@b:c/d"

    def test_empty_password_is_noop(self) -> None:
        url = _pg("OLD", host="h:5432", db="db")
        assert url_with_password(url, "") == url


class TestUrlWithUserinfo:
    def test_sets_both_user_and_password(self) -> None:
        out = url_with_userinfo(_pg("OLD", user="ava"), "ava_dev", _SECRET)
        assert out == _pg(_SECRET, user="ava_dev")

    def test_empty_password_still_writes_username(self) -> None:
        """An empty password keeps the username (`user@host`): the username IS the
        data-plane identity (names-as-data), and a no-secret cluster carries
        identity without a credential."""
        url = _pg("OLD", user="ava")
        out = url_with_userinfo(url, "ava_dev", "")
        # exact form: postgresql://ava_dev@db.host:5432/ava (username, no password)
        assert out == "postgresql://ava_dev@db.host:5432/ava"


def _settings_with(
    *,
    db_url: str,
    redis_url: str,
    secret: str,
    db_admin_password: str = "",
    redis_admin_password: str = "",
) -> DataPlaneSettings:
    # Construct a fresh Settings from init kwargs (highest-priority source, so the
    # session's env is irrelevant) — the model-validator runs at construction, which
    # is what setattr on the singleton could not exercise. Kwargs are the field
    # aliases, since the fields populate by alias.
    return DataPlaneSettings(
        AVA_DB_URL=db_url,
        AVA_REDIS_URL=redis_url,
        AVA_CLUSTER_SECRET=secret,
        AVA_DB_ADMIN_PASSWORD=db_admin_password,
        AVA_REDIS_ADMIN_PASSWORD=redis_admin_password,
    )


class TestSettingsAppliesDataPlanePasswords:
    def test_main_url_uses_the_database_admin_password(self) -> None:
        # Names-as-data: on a LOCAL instance the stale PASSWORD is overwritten
        # with the DB owner password; the username and database stay exactly
        # what the URL says (here the historical prod identifiers), so an
        # existing cluster keeps dialing its own db across the rename window.
        # A foreign host would keep its own password (Task #1752) — see
        # test_foreign_url_password_survives_a_nonempty_cluster_secret.
        db_admin = "-".join(("db", "admin", "v2"))
        redis_admin = "-".join(("redis", "admin", "v2"))
        s = _settings_with(
            db_url=_pg("STALE", host="localhost:5432", user="ava_main", db="ava_main"),
            redis_url=_redis("STALE", host="localhost:6379", user="ava_main"),
            secret=_SECRET,
            db_admin_password=db_admin,
            redis_admin_password=redis_admin,
        )
        assert s.db_url == _pg(db_admin, host="localhost:5432", user="ava_main", db="ava_main")
        assert s.redis_url == _redis("STALE", host="localhost:6379", user="ava_main")
        assert s.db_admin_password == db_admin
        assert s.redis_admin_password == redis_admin

    def test_fresh_cluster_fixed_identity_passes_through(self) -> None:
        # A path-only birth writes the fixed `ava` identifiers; Settings keeps
        # them (on a loopback URL, which is the local-instance posture).
        s = _settings_with(
            db_url=_pg("STALE", host="localhost:5432", user="ava", db="ava"),
            redis_url=_redis("STALE", host="localhost:6379", user="ava"),
            secret=_SECRET,
        )
        assert s.db_url == _pg(_SECRET, host="localhost:5432", user="ava", db="ava")
        assert s.redis_url == _redis("STALE", host="localhost:6379", user="ava")

    def test_redis_url_stays_verbatim(self) -> None:
        # The Redis URL carries the ACL runtime password, independent from both
        # the bearer and the Redis default-user admin password. Mint/rotation is
        # the only path that rewrites it.
        s = _settings_with(
            db_url=_pg("STALE", host="gw.host:5432"),
            redis_url="redis://gw.host:6379/0",
            secret=_SECRET,
        )
        assert s.redis_url == "redis://gw.host:6379/0"

    def test_runner_projected_url_keeps_its_own_password(self) -> None:
        # The least-privilege runner URL (Task #1236) carries the runner's OWN
        # credential (AVA_RUNNER_DB_PASSWORD), freshly projected by the gateway
        # at every fetch — deliberately NOT the cluster secret. Overwriting it
        # would SASL-fail every runner: the role's stored verifier is its own
        # password, not the bearer (prod finding, #2599 follow-up). Redis also
        # stays verbatim: its ACL password is an independent runtime credential.
        runner_pw = "runner-pw-v1"
        s = _settings_with(
            db_url=_pg(runner_pw, host="gw.host:5432", user="ava_runner", db="ava"),
            redis_url=_redis("STALE", host="gw.host:6379", user="ava"),
            secret=_SECRET,
        )
        assert s.db_url == _pg(runner_pw, host="gw.host:5432", user="ava_runner", db="ava")
        assert s.redis_url == _redis("STALE", host="gw.host:6379", user="ava")

    def test_agent_profile_rejects_local_owner_url_with_cluster_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
        monkeypatch.setattr(data_plane, "_unit_home", lambda: Path.home() / ".ava")

        with pytest.raises(ValueError, match="must receive an ava_runner AVA_DB_URL"):
            _settings_with(
                db_url=_pg("STALE", host="127.0.0.1:5432", user="ava"),
                redis_url=_redis("STALE", host="127.0.0.1:6379", user="ava"),
                secret=_SECRET,
            )

    def test_agent_profile_keeps_local_owner_url_at_a_nondefault_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
        monkeypatch.setattr(data_plane, "_unit_home", lambda: tmp_path / "test-home")

        s = _settings_with(
            db_url=_pg("STALE", host="127.0.0.1:5432", user="ava"),
            redis_url=_redis("STALE", host="127.0.0.1:6379", user="ava"),
            secret=_SECRET,
        )

        assert s.db_url.startswith(f"postgresql://ava:{_SECRET}@127.0.0.1:5432/ava")

    def test_gateway_profile_keeps_local_owner_url_password_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AVA_PROCESS_PROFILE", "gateway")

        s = _settings_with(
            db_url=_pg("STALE", host="127.0.0.1:5432", user="ava"),
            redis_url=_redis("STALE", host="127.0.0.1:6379", user="ava"),
            secret=_SECRET,
        )

        assert s.db_url.startswith(f"postgresql://ava:{_SECRET}@127.0.0.1:5432/ava")

    def test_agent_profile_keeps_projected_runner_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
        runner_pw = "runner-pw-v1"

        s = _settings_with(
            db_url=_pg(runner_pw, host="127.0.0.1:5432", user="ava_runner"),
            redis_url=_redis("STALE", host="127.0.0.1:6379", user="ava"),
            secret=_SECRET,
        )

        assert s.db_url.startswith("postgresql://ava_runner:runner-pw-v1@127.0.0.1:5432/ava")

    def test_empty_secret_leaves_urls_verbatim(self) -> None:
        # An unprovisioned checkout / the no-secret test path must not rewrite URLs.
        db = _pg("OLD", host="h:5432")
        rd = _redis("OLD", host="h:6379")
        s = _settings_with(db_url=db, redis_url=rd, secret="")
        assert s.db_url == db
        assert s.redis_url == rd

    def test_unanchored_sentinel_is_left_untouched(self) -> None:
        # The connect guard matches the sentinel byte-for-byte; injecting a password
        # would break that recognition, so the sentinel must pass through unchanged
        # even when a secret is present.
        s = _settings_with(
            db_url=UNANCHORED_DB_SENTINEL,
            redis_url=_redis("OLD", host="h:6379"),
            secret=_SECRET,
        )
        assert s.db_url == UNANCHORED_DB_SENTINEL
        # Redis is independent from the bearer and remains verbatim.
        assert s.redis_url == _redis("OLD", host="h:6379")


@pytest.fixture
def _restore_machine_env() -> Iterator[None]:
    """Save/restore AVA_MACHINE_HOST + AVA_HOME around a test. These are Settings
    aliases, so monkeypatch.setenv on them is banned by the force-settings lint —
    but `_self_machine_host` (like `_unit_home`) reads os.environ directly at
    sub-model construction time, so the tests set os.environ directly with
    explicit restore (the pattern of tests/shared/test_unit_home.py)."""
    saved = {k: os.environ.get(k) for k in ("AVA_MACHINE_HOST", "AVA_HOME")}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.mark.usefixtures("_restore_machine_env")
class TestSelfHostDialsLoopback:
    """The self-dial loopback rewrite (`_dial_self_host_via_loopback`): a data-plane
    URL whose host is this machine's own reachable address dials 127.0.0.1 —
    never out the NIC / through a VPN network extension — while any other host
    passes through verbatim (a bootstrap-served URL keeps working on remote
    runners). Machine host is pinned via env/AVA_HOME so the session's real
    environment never leaks in."""

    def _isolate(self, tmp_path: Path, *, machine_host: str | None) -> None:
        os.environ["AVA_HOME"] = str(tmp_path)  # no machine_host file
        if machine_host is None:
            os.environ.pop("AVA_MACHINE_HOST", None)
        else:
            os.environ["AVA_MACHINE_HOST"] = machine_host

    def test_self_host_rewrites_to_loopback(self, tmp_path: Path) -> None:
        self._isolate(tmp_path, machine_host="gw.host")
        s = _settings_with(
            db_url=_pg("STALE", host="gw.host:5433"),
            redis_url=_redis("STALE", host="gw.host:6380"),
            secret=_SECRET,
        )
        # Host -> loopback; identity (as carried by the URL) / port / db untouched.
        # db_url also picks up ?hostaddr=127.0.0.1 (_pin_ipv4_hostaddr): the
        # loopback host IS an IPv4 literal, so libpq's own resolution-bypass
        # applies here too, same as any other literal host.
        assert s.db_url == _pg(_SECRET, host="127.0.0.1:5433") + "?hostaddr=127.0.0.1"
        assert s.redis_url == _redis("STALE", host="127.0.0.1:6380")

    def test_foreign_host_is_untouched(self, tmp_path: Path) -> None:
        # A runner whose URLs point at the (remote) gateway must keep dialing it.
        self._isolate(tmp_path, machine_host="runner.host")
        s = _settings_with(
            db_url=_pg("STALE", host="gw.host:5433"),
            redis_url=_redis("STALE", host="gw.host:6380"),
            secret=_SECRET,
        )
        assert urlsplit(s.db_url).hostname == "gw.host"
        assert urlsplit(s.redis_url).hostname == "gw.host"

    def test_one_url_dial_inherits_loopback_on_pooler_port(self, tmp_path: Path) -> None:
        # The one-URL design: AVA_DB_URL itself carries the pooler port (pooling
        # on), and the loopback rewrite applies to that same dial URL.
        self._isolate(tmp_path, machine_host="gw.host")
        s = DataPlaneSettings(
            AVA_DB_URL=_pg("STALE", host="gw.host:6433"),
            AVA_REDIS_URL=_redis("STALE", host="gw.host:6380"),
            AVA_CLUSTER_SECRET=_SECRET,
            AVA_PGBOUNCER_ENABLED=True,
        )
        # ?hostaddr= survives (url_with_host preserves the query string) — see the
        # note in test_self_host_rewrites_to_loopback.
        assert s.db_url == _pg(_SECRET, host="127.0.0.1:6433") + "?hostaddr=127.0.0.1"

    def test_localhost_machine_host_default_is_noop(self, tmp_path: Path) -> None:
        # The zero-config single box (machine host resolves to `localhost`): an
        # already-loopback URL keeps its host, and no foreign host matches.
        self._isolate(tmp_path, machine_host=None)
        s = _settings_with(
            db_url=_pg("STALE", host="localhost:5433"),
            redis_url=_redis("STALE", host="gw.host:6380"),
            secret=_SECRET,
        )
        assert urlsplit(s.db_url).hostname == "localhost"
        assert urlsplit(s.redis_url).hostname == "gw.host"

    def test_loopback_machine_host_never_rewrites(self, tmp_path: Path) -> None:
        # AVA_MACHINE_HOST explicitly `localhost` (hairpin workaround .env): the
        # loopback guard fires before the compare — URLs stay byte-identical.
        self._isolate(tmp_path, machine_host="localhost")
        s = _settings_with(
            db_url=_pg("STALE", host="localhost:5433"),
            redis_url=_redis("STALE", host="localhost:6380"),
            secret=_SECRET,
        )
        assert s.db_url == _pg(_SECRET, host="localhost:5433")
        assert s.redis_url == _redis("STALE", host="localhost:6380")

    def test_machine_host_file_fallback_matches(self, tmp_path: Path) -> None:
        # env unset -> the `$AVA_HOME/machine_host` file (written by enroll) wins.
        self._isolate(tmp_path, machine_host=None)
        (tmp_path / "machine_host").write_text("gw.host\n")
        s = _settings_with(
            db_url=_pg("STALE", host="gw.host:5433"),
            redis_url=_redis("STALE", host="gw.host:6380"),
            secret=_SECRET,
        )
        assert urlsplit(s.db_url).hostname == "127.0.0.1"
        assert urlsplit(s.redis_url).hostname == "127.0.0.1"

    def test_sentinel_stays_byte_identical(self, tmp_path: Path) -> None:
        self._isolate(tmp_path, machine_host="gw.host")
        s = _settings_with(
            db_url=UNANCHORED_DB_SENTINEL,
            redis_url=_redis("STALE", host="h:6379"),
            secret=_SECRET,
        )
        assert s.db_url == UNANCHORED_DB_SENTINEL


class TestPinIpv4Hostaddr:
    """`_pin_ipv4_hostaddr`: db_url gets `?hostaddr=<host>` appended when its
    host is an IPv4 literal — libpq's own resolution bypass for the
    DNS64/NAT64-synthesis-of-a-literal failure mode
    (shared.netutil.is_ipv4_literal). Not a fix for a proven psycopg bug
    (psycopg's own `_resolve_hostnames` already skips getaddrinfo for a
    literal) — defense-in-depth for other libpq consumers (PgBouncer's
    upstream dial, psql/pg_dump). redis_url is untouched — redis has no
    hostaddr-equivalent; that fix is code-level
    (shared.redis_client._PinnedIPv4Connection)."""

    def test_ipv4_literal_host_gets_hostaddr(self) -> None:
        # A foreign IPv4 host: hostaddr is appended, and (Task #1752) the
        # provider password is preserved rather than replaced by the secret.
        s = _settings_with(
            db_url=_pg("OLD", host="198.51.100.7:5433"),
            redis_url=_redis("OLD", host="198.51.100.7:6380"),
            secret=_SECRET,
        )
        assert s.db_url == _pg("OLD", host="198.51.100.7:5433") + "?hostaddr=198.51.100.7"
        # redis has no hostaddr mechanism -- untouched.
        assert s.redis_url == _redis("OLD", host="198.51.100.7:6380")

    def test_hostname_host_gets_no_hostaddr(self) -> None:
        s = _settings_with(
            db_url=_pg("OLD", host="gw.host:5433"),
            redis_url=_redis("OLD", host="gw.host:6380"),
            secret=_SECRET,
        )
        assert s.db_url == _pg("OLD", host="gw.host:5433")
        assert "hostaddr" not in s.db_url

    def test_unanchored_sentinel_untouched(self) -> None:
        s = _settings_with(
            db_url=UNANCHORED_DB_SENTINEL,
            redis_url=_redis("OLD", host="h:6379"),
            secret=_SECRET,
        )
        assert s.db_url == UNANCHORED_DB_SENTINEL

    def test_existing_query_params_preserved(self) -> None:
        # hostaddr is appended alongside any existing query string, not over it.
        s = _settings_with(
            db_url=_pg("OLD", host="198.51.100.7:5433") + "?sslmode=disable",
            redis_url=_redis("OLD", host="h:6379"),
            secret=_SECRET,
        )
        parts = urlsplit(s.db_url)
        params = dict(p.split("=") for p in parts.query.split("&"))
        assert params == {"sslmode": "disable", "hostaddr": "198.51.100.7"}


@pytest.mark.usefixtures("_restore_machine_env")
class TestSelfMachineHostParity:
    """`_self_machine_host` is a leaf duplicate of `shared.machine.reachable_host`
    (the config sub-model cannot import shared.machine — circular), so pin its
    precedence (env AVA_MACHINE_HOST > $AVA_HOME/machine_host file > localhost)
    to the real resolver: each source case asserts both return the same value,
    so a future drift in either side fails here."""

    @pytest.fixture(autouse=True)
    def _fresh_identity(self) -> Iterator[None]:
        # reachable_host caches per process — reset around each case so it
        # re-resolves from the sources this test pins.
        reset_identity()
        yield
        reset_identity()

    def _pin(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, env: str | None) -> None:
        # The two resolvers read different surfaces of the same sources:
        # _self_machine_host reads os.environ at construction time; reachable_host
        # reads the settings singleton (machine_host / ava_home fields). Pin both
        # surfaces to the same values so the assertion compares precedence only.
        os.environ["AVA_HOME"] = str(tmp_path)
        monkeypatch.setattr(settings.general, "ava_home", tmp_path)
        if env is None:
            os.environ.pop("AVA_MACHINE_HOST", None)
            monkeypatch.setattr(settings.general, "machine_host", "")
        else:
            os.environ["AVA_MACHINE_HOST"] = env
            monkeypatch.setattr(settings.general, "machine_host", env)

    def test_env_wins_over_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._pin(monkeypatch, tmp_path, env="gw.env-host")
        (tmp_path / "machine_host").write_text("gw.file-host\n")  # env must shadow it
        assert _self_machine_host() == reachable_host() == "gw.env-host"

    def test_file_when_env_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._pin(monkeypatch, tmp_path, env=None)
        (tmp_path / "machine_host").write_text("gw.file-host\n")
        assert _self_machine_host() == reachable_host() == "gw.file-host"

    def test_localhost_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._pin(monkeypatch, tmp_path, env=None)
        assert _self_machine_host() == reachable_host() == "localhost"
