"""Embedding providers behind the memory index.

One protocol (`embeddings.base.EmbeddingProvider`), N implementations
(Gemini today — the pre-abstraction `embedder` logic moved to
`embeddings.gemini` unchanged), one switch
(`embeddings.factory.get_provider()` reading `AVA_EMBEDDING_BACKEND`).
The indexer daemon (write path) and the gateway search endpoint (read
path) both take their provider from the factory; neither imports a
concrete provider directly.

The provider declares the vector space (`dim` + `fingerprint`); the
storage backends take both at construction through
`backends.factory.get_backend(...)` — no provider constant is imported by
a backend (the pre-abstraction `embedder.DIM` coupling is gone).
"""
