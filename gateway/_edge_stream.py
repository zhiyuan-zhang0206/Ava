"""Edge-stream constants shared by the fleet graph and the neighbors walk.

Both readers query the same audit edge events from Loki
(gateway/routers/fleet_graph.py and gateway/neighbors.py); defining the
constants once here keeps the two readers from drifting.
"""

# Audit event names that form edges. Lineage (spawn/fork/resurrect) is
# permanent and all-time; messages (send_message) decay with recency.
LINEAGE_EVENT_NAMES = ("spawn", "fork", "resurrect")
EDGE_EVENT_NAMES = ("send_message", *LINEAGE_EVENT_NAMES)

# Loki fetch cap for the edge stream. Audit events are low-volume (a few
# thousand since the cutover); the cap is a guardrail, not an expectation - a
# truncated read degrades to a partial graph. Protective constant (task #3696
# exception inventory: KEEP).
LOKI_EDGE_LIMIT = 50_000
