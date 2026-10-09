# Agent view: any agents on one timeline, no second page

Decision (2026-10-09): the run timeline page is the agent view. Any number of
agents, added and removed by id, share one time axis; one agent is the same page
with one group. All agents are equal (no root, no lineage), each keeps all of its
own rows, and nothing is drawn between agents. Settings apply to every group: how
many tree levels, and which context bars (off / absolute / added / both).

Why: comparing agents (a coordinator and its workers) needs their rows on one
axis; a separate cluster page or a second single-agent page would duplicate the
canvas, selection and keyboard model.

Axis: time only. A token (hybrid) axis was built first and removed after review
(no use for it in the view; its code, control and strings are gone). Context bars
default to added.

Mechanics: each agent is read by the existing per-agent `run-timeline` endpoint,
so no new cluster route. A selection is `{agent, selection}` because node ids and
block indices repeat across agents. Arrow keys continue from an agent's last row into
the next agent's first.

Rejected: a cluster-level endpoint (one more read model to keep consistent with
the per-agent one); lineage/root structure (agents are compared, not traversed);
squeezing an agent to one row (hides the tree the page exists for).

Cost accepted: the history cache holds one whole-history view per agent
(about 12 KB per message, measured: 4.6k messages 49 MB, 200 messages 3 MB), so
its cap went from 6 to 16 entries; a view of more agents than the cap rebuilds
evicted ones on each read.
