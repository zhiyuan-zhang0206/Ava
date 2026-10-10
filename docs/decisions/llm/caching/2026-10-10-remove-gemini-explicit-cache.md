# Remove Gemini explicit caching

We removed Ava's Gemini CachedContent lifecycle and its opt-in configuration.
The operator observed that enabling explicit caching interfered with the
implicit caching behavior needed by the workload and chose to remove the
feature rather than retain a disabled compatibility path.

Gemini now uses ordinary tool-bound requests with the complete SystemMessage
and conversation prefix. The Google provider, model configuration, implicit
cache-read token accounting and other providers retain their existing behavior.
The Google-only preparation/recovery contract and hierarchy's Gemini exclusion
were removed with their consumers. Historical usage provenance remains readable.

We rejected replacing the process-wide memo dictionaries with a shared cache
owner: that would preserve a feature the operator explicitly retired.
