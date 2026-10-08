# Self-evolution method context

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Backpropagation analogy

The evaluation loop optimizes skill text the way backpropagation optimizes
weights:

| ML concept | Self-evolution equivalent |
|-----------|--------------------------|
| Forward pass | Agent runs task with current skill |
| Loss | `rubric.py` scores (completion + efficiency) |
| **Backward pass (gradient)** | **Ask the agent directly: "what should change?"** |
| Weight update | Edit the skill text |

The backward pass is **80% agent self-reflection** (`evaluate.debrief()`)
and **20% trace-mining by a separate worker** — the agent that ran the task
knows best what tripped it up.
