"""The understanding tree: how an agent's history becomes a tree of summaries.

`units.py` divides messages into layer-0 units (and the blocks the run-timeline draws), `chunks.py`
and `chunk_consumer.py` describe a stretch of the agent's own context in one cached call (groups of
units and a summary each, `chunk_generate.py`, `leaf_groups.py`), `group.py`, `group_consumer.py`
and `group_store.py` group the levels above, `store.py` / `serve.py` / `usage.py` read the tree and
its costs for the run-timeline, and `generate.py` is the provider-call layer. Each page of the
package's `docs/` says one part.
"""
