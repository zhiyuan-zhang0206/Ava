#!/usr/bin/env bash
# Postgres 17 + Redis SERVER — the native data plane a gateway owns (and the CI /
# bench hosts need so the test suite can spin throwaway clusters). One source for
# install.sh / install-system.sh. Linux: PGDG pg17 + redis-server; macOS: brew;
# Windows: Docker (see docker-compose.windows.yml).
#
# WSL without sudo: a Linux host that already carries the whole data plane
# (pg17 binaries under /usr/lib/postgresql/17/bin, redis-server, pgbouncer)
# skips apt entirely — the keyring/apt-sources writes need root, and the
# runtime provisions its own per-cluster instance under $AVA_HOME/pg from a
# cached template (cli/commands/_cluster_instance.py:_ensure_pg_data), so there
# is no system data dir to bootstrap. The former `--initdb` block that initdb'd
# /var/lib/postgresql/17/data (a legacy path `ava start` never reads) is
# removed. An apt failure degrades to a warning; the packages can be installed
# later via `sudo apt-get install ...`.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

OS="$(prov_os)"
case "$OS" in
  linux)
    # Presence gate: skip apt when the data plane is already installed. The
    # runtime needs the server binaries only; per-cluster data dirs are
    # provisioned under $AVA_HOME by `ava start`.
    if [ -x /usr/lib/postgresql/17/bin/initdb ] \
        && command -v redis-server >/dev/null 2>&1 \
        && command -v pgbouncer >/dev/null 2>&1; then
      prov_log "pg17 + redis + pgbouncer already present — skipping apt (works without sudo)"
    elif ! prov_sudo apt-get update; then
      prov_log "WARNING apt-get update failed (no passwordless sudo?) — data-plane packages not installed."
      prov_log "  Install them later with: sudo apt-get install -y postgresql-17 redis pgbouncer"
    else
      prov_apt_install ca-certificates curl gnupg locales

      # Redis >= 6.2 for ACL resetchannels (Ubuntu 22.04 apt ships 6.0).
      # Install from the official redis.io apt repo (matching prod's 8.2).
      prov_apt_install gpg
      curl -fsSL https://packages.redis.io/gpg | gpg --dearmor > /tmp/redis-archive-keyring.gpg
      prov_sudo mv /tmp/redis-archive-keyring.gpg /usr/share/keyrings/redis-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(source /etc/os-release && echo $VERSION_CODENAME) main"       | prov_sudo tee /etc/apt/sources.list.d/redis.list > /dev/null
      prov_sudo apt-get update
      prov_apt_install redis
      prov_sudo locale-gen en_US.UTF-8

      # PgBouncer transaction pooler (per-cluster data plane, on by default;
      # AVA_PGBOUNCER_ENABLED=false is the kill-switch). apt's build (>= 1.14)
      # supports scram-sha-256 client auth.
      prov_apt_install pgbouncer

      # Postgres 17 server via the official PGDG repo (Ubuntu 24.04 ships 16).
      # initdb/pg_ctl land in /usr/lib/postgresql/17/bin (not on PATH; the test
      # fixture + ava start resolve them there). Client tools (psql/pg_dump)
      # ride along as a dependency.
      prov_apt_install postgresql-common
      prov_sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
      prov_apt_install postgresql-17
    fi
    ;;
  macos)
    # brew runs initdb for postgresql@17 with the current user as the bootstrap
    # superuser; ava start provisions the per-cluster role + db on first boot
    # (cli/commands/_cluster_instance.py) and drives pg via pg_ctl, not brew
    # services.
    brew install postgresql@17 redis pgbouncer
    ;;
  windows)
    # PostgreSQL and Redis are provided via Docker Desktop (WSL2 backend).
    # Native Windows PG/Redis installation is deferred to Phase 3.
    # See docker-compose.windows.yml and conventions/windows-setup.md.
    prov_log "Windows: PostgreSQL + Redis are expected via Docker Desktop (docker-compose.windows.yml)"
    prov_log "If Docker is not running, start Docker Desktop and run: docker compose -f docker-compose.windows.yml up -d"
    ;;
esac
prov_log "postgres 17 + redis + pgbouncer installed"
