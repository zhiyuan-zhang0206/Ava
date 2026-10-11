"""The node names the llm node may route to."""

from typing import Literal

# llm_node normal → BEFORE_EXEC; cancel / no-tool-call halt → AFTER_EXEC
# (halted=True makes after_exec route back to claim). Type narrow catches illegal goto.
LlmGoto = Literal["before_exec", "after_exec", "init_context", "claim"]
