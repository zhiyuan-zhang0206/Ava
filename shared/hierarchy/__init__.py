"""Hierarchical understanding engine — blocks, seal cascade, generation.

The engine turns one agent's message stream into a level tree of summaries:
`blocks.py` folds the console item stream into "think -> act -> observe" units,
`seal.py` cuts the unit stream into sealable groups at trigger points and fixes
every node's narrative budget, `generate.py` materializes the node texts via
one bounded model call each, `tokens.py` is the token caliber all budgets use.

`ENGINE_VERSION` names the semantics of that pipeline as a whole; stored node
rows record it (with `PROMPT_VERSION` and the storage schema version) so a
generation can always be traced to the rules that produced it.
"""

from __future__ import annotations

ENGINE_VERSION = "0.3"
