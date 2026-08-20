-- Reverse of the extension registry tables. Content is materialized on every
-- machine already and the per-machine caches (installed.json,
-- plugins_config.json) are still valid pre-migration authorities, so dropping
-- these loses the cluster-level view and nothing else.
DROP INDEX IF EXISTS idx_extensions_enabled_kind;
DROP TABLE IF EXISTS extensions;
DROP TABLE IF EXISTS extension_blobs;
