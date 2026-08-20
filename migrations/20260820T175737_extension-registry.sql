-- Extension registry: the cluster owns which extensions exist and their default
-- enablement; the machine owns only capabilities.
-- Slice S2 of future/infra/extension-ownership.md (issue #39); the ownership
-- model is decisions/2026-08-21-extension-ownership-three-tiers.md.
--
-- Content-addressed blobs come FIRST: `extensions.content_hash` references them,
-- and a row may only exist for content that has already landed.

CREATE TABLE extension_blobs (
    content_hash TEXT PRIMARY KEY,      -- shared.install_registry.tree_hash of the landed tree
    archive      BYTEA NOT NULL,        -- tar of that tree, IGNORED_NAMES excluded
    size_bytes   INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The cap is a CONSTRAINT, not a convention. Extension content is source
    -- trees (markdown, a little Python); large artifacts are host provisioning
    -- and do not belong in the cluster's data plane. 8 MiB is far above any
    -- real package and far below "someone put a model checkpoint in Postgres".
    -- shared/extension_registry.py:MAX_BLOB_BYTES carries the same number and
    -- tests/shared/test_extension_registry.py pins the two together by writing
    -- exactly the cap and exactly one byte over.
    CONSTRAINT extension_blobs_size_cap CHECK (size_bytes > 0 AND size_bytes <= 8388608),
    -- The declared size must BE the archive's size — otherwise the cap is
    -- checked against a number the writer chose rather than the bytes stored.
    CONSTRAINT extension_blobs_size_is_real CHECK (size_bytes = octet_length(archive))
);

CREATE TABLE extensions (
    name            TEXT PRIMARY KEY,   -- match_key-folded (dash/underscore are one name)
    kind            TEXT NOT NULL CHECK (kind IN ('skill', 'plugin', 'mcp')),
    source          TEXT NOT NULL,      -- 'repo' | git URL | 'local:<machine>'
    source_ref      TEXT,               -- commit/tag as installed, when source is git
    version         TEXT,               -- manifest version, when the package declares one
    content_hash    TEXT REFERENCES extension_blobs(content_hash),
    manifest        JSONB,              -- ava-plugin.json as landed
    trust           TEXT NOT NULL DEFAULT 'unreviewed'
                    CHECK (trust IN ('builtin', 'reviewed', 'unreviewed')),
    default_enabled BOOLEAN NOT NULL DEFAULT true,
    installed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Repo-shipped content does NOT ride the data plane: it is already
    -- cluster-consistent via commit-pinned rollout, and its trust story is the
    -- checkout. A 'repo' row exists only to carry `default_enabled`, so it must
    -- have no blob; everything that arrived by INSTALL must have one. Encoding
    -- it here makes "the registry owns what arrives by install, not by release"
    -- a schema fact rather than a sentence in a design doc.
    CONSTRAINT extensions_blob_iff_installed CHECK (
        (source = 'repo' AND content_hash IS NULL)
        OR (source <> 'repo' AND content_hash IS NOT NULL)
    )
);

-- The materialization query is "what should this machine have", which reads the
-- enabled rows; kind narrows it per slice (S2 materializes only skills).
CREATE INDEX idx_extensions_enabled_kind ON extensions (kind) WHERE default_enabled;
