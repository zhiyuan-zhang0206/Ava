"""NumPy memory search service — the lightweight exact-search backend.

An independent local process (`python -m services.derived.memory_search.daemon`)
serving the memory search API over HTTP on 19531, so the indexer daemon
and the gateway both talk to it over HTTP — no cross-process shared state.

Storage is an in-memory float32 matrix (the pool's ~2k chunk rows x 3072
dims ≈ 24MB) persisted as a single `vectors.npz` under
`$AVA_HOME/memory-search/` with an atomic-rename rewrite on every
mutation (<1s at this scale). Search is one exact matrix product over
every row, aggregated per path — an exact scan, no approximate index.
"""
