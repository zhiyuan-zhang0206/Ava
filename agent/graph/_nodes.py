"""Backward-compat re-export — node name constants moved to `agent/nodes.py`.

The constants live outside `agent/graph/` so `agent.hooks._registry` can
import NodeName without running this package's __init__ (graph ⇄ hooks import
cycle; see `agent/nodes.py` docstring). This module remains as a re-export so
existing `from agent.graph._nodes import ...` callers don't break. New code
should import directly from `agent.nodes`.
"""

from agent.nodes import AFTER_EXEC as AFTER_EXEC
from agent.nodes import AFTER_INIT as AFTER_INIT
from agent.nodes import BEFORE_EXEC as BEFORE_EXEC
from agent.nodes import BEFORE_LLM as BEFORE_LLM
from agent.nodes import CLAIM as CLAIM
from agent.nodes import END as END
from agent.nodes import EXEC as EXEC
from agent.nodes import INIT_CONTEXT as INIT_CONTEXT
from agent.nodes import LLM as LLM
from agent.nodes import NodeName as NodeName
