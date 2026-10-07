# The Linux verification container: one Ava machine, born fresh for each run.
#
# Design: future/infra/engineering/verification-boundaries.md. Build and run through
# `python3 scripts/verify/container.py`; this file is its image definition.
#
# The image is the machine, not the software under test. It carries the system
# layer from the repository's own provisioning scripts (Python 3.12, PostgreSQL 17
# with pgvector, Redis 8.2, PgBouncer at the pinned build, Node 22, the CLI tools)
# and uv, and runs as an ordinary user: PostgreSQL refuses to run as root. It holds
# no source checkout, no virtualenv, no node_modules and no secret; the run clones
# the commit under test into ~/.ava/source and builds both dependency trees there
# from that commit's lockfiles.
#
# The build context is the repository root, filtered by Dockerfile.dockerignore to
# the provisioning scripts. They are copied to the layout install-system.sh expects
# (it reaches ../install-cli-tools.sh from its own directory).

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

COPY scripts/provision /opt/ava-provision/scripts/provision
COPY scripts/install-cli-tools.sh /opt/ava-provision/scripts/install-cli-tools.sh

# The provisioning script is written for a host with apt and (when not root)
# passwordless sudo; the build is root, so it needs neither. The apt lists are
# removed afterwards to keep the layer small.
RUN bash /opt/ava-provision/scripts/provision/install-system.sh \
    && rm -rf /var/lib/apt/lists/*

# A real passwd entry with a home: the data plane's administrator is the OS user
# over `peer` authentication on an owner-only socket. Not named `ava`: initdb makes
# the OS user the bootstrap superuser, and start refuses a superuser that shares its
# name with the schema owner role `ava`.
RUN useradd --create-home --shell /bin/bash verify

USER verify
WORKDIR /home/verify

# uv, at the pinned release, into ~/.local/bin. One cache directory (mounted as a
# named volume by the run script) holds the uv package cache, the managed Python
# interpreters and the npm cache; it carries no cluster state.
ENV PATH=/home/verify/.local/bin:${PATH} \
    UV_CACHE_DIR=/home/verify/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/home/verify/.cache/uv-python \
    npm_config_cache=/home/verify/.cache/npm
RUN bash /opt/ava-provision/scripts/provision/toolchain.sh \
    && mkdir -p /home/verify/.cache

# `docker run --init` supplies PID 1 (ava-root detaches into its own session and is
# reparented there, so PID 1 must reap). Nothing starts by itself.
CMD ["sleep", "infinity"]
