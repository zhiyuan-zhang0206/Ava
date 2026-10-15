# Review packet

These are constructed records for evaluating the workflow instructions.

There were 200 support conversations last week. A colleague selected 20 with
complaints or unusually long latency. Humans marked 18 of those 20 as failures.
The other 180 conversations have not been reviewed.

A separate judge check used 20 labeled examples: 19 human Pass and one human
Fail. The judge returned Pass for all 20. These same examples supplied its
few-shot prompt. No independent judge test has been run.

Old and new instructions were replayed once on the same two tasks. The mean
completion score rose from 0.80 to 1.00. Completion uses only nonempty final
output, execution success, and a clean ending. Model settings and instruction
snapshots were not recorded.

## Case A

User: "Has invoice INV-17 been paid?"
Tool request: lookup_invoice("INV-17")
Tool response: {"id": "INV-17", "status": "unpaid", "balance": 125}
Assistant: "Yes, INV-17 has been paid in full."
Execution errors: 0. Final output: nonempty. Completion score: 1.00.
Human note: "This would cause us to stop chasing a payment we are still owed."

## Case B

User: "Has invoice INV-22 been paid?"
Tool request: lookup_invoice("INV-22")
Tool response: {"id": "INV-22", "status": "paid", "balance": 0}
Assistant: "Yes, INV-22 has been paid in full."
Execution errors: 0. Final output: nonempty. Completion score: 1.00.
Human note: "Accurate."
