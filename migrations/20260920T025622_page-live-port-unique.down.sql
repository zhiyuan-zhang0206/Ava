-- Reverse of page-live-port-unique: drop the index. The duplicate rows the
-- forward migration closed stay closed — which of them was actually serving
-- is recorded nowhere, and re-opening a duplicate would re-create the very
-- conflict the index exists to prevent.
DROP INDEX IF EXISTS agent_pages_unique_live_port;
