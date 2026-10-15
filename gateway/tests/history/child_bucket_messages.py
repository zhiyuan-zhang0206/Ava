import sys

from langchain_core.messages import HumanMessage

from agent.messages import COMPACT_SUMMARY_HEADER
from base.agents.history.context_breakdown import bucket_messages
from base.agents.history.message_tokens import MessageTokens

if __name__ == "__main__":
    buckets = bucket_messages(
        [HumanMessage(content=f"{COMPACT_SUMMARY_HEADER}\n\nbody")],
        [MessageTokens(7, None, "exact")],
    )
    assert list(buckets) == ["compact_summary"], buckets
    assert "agent.graph" not in sys.modules, "breakdown must not import agent.graph"
