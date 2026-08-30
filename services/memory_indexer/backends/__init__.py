"""Storage backends behind the memory search index.

One protocol (`backends.base.MemorySearchBackend`), N implementations
(milvus, numpy, pgvector), one switch
(`backends.factory.get_backend()` reading `AVA_MEMORY_SEARCH_BACKEND`).
The indexer daemon (write path) and the gateway search endpoint (read
path) both take their backend from the factory; neither imports a
concrete backend directly. The embedding vector space (dim + provider
fingerprint) is injected by the factory from `embeddings.get_provider()`
— backends never import provider constants.
"""
