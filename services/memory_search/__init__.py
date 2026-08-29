"""NumPy memory search service — the lightweight exact-search backend.

An independent local process (`python -m services.memory_search.daemon`)
serving the memory search API over HTTP on 19531 (one past milvus's
19530), so the indexer daemon and the gateway both talk to it exactly
like they talk to the milvus daemon — no cross-process shared state.

Storage is an in-memory float32 matrix (the pool's ~2k chunk rows x 3072
dims ≈ 24MB) persisted as a single `vectors.npz` under
`$AVA_HOME/memory-search/` with an atomic-rename rewrite on every
mutation (<1s at this scale). Search is one exact matrix product over
every row, aggregated per path — no approximate index, so its results
are the reconciliation baseline for the approximate backends.
"""
