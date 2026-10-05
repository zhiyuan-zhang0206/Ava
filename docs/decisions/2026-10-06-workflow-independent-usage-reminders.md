# Workflow composition and usage reminders are independent of Fleet

The user chose to keep Workflow's methods independent of Fleet. Workflow describes
intent, responsibilities, coordination, recovery, and evidence in natural
language. Fleet offers optional collaboration conveniences such as labels and
task records. Sharing the core repository permits composition through component
contracts; it does not erase the runtime availability boundary of a disabled
plugin. Execution recipes own their concrete APIs.

We removed task-based LLM cost attribution. A task can involve many peers, and a
persistent peer can work on many tasks; a task note or owner cannot establish
which consumption belongs to which objective. Neither an inferred owner nor one
active task ID represents that many-to-many relationship faithfully.

Instead an ordinary script reports recorded usage for explicit agent IDs, a
window or lifetime, and selected birth ancestry. Spawn edges and fork-source
edges can be selected separately or together. These are agent-scope statistics;
reused peers may have unrelated work in the same scope. The agent chooses the
scope appropriate to the question. Usage-time prices and unpriced-call counts
remain the accounting facts; this is not a ledger of all business expenses.

Budget reminders follow the same pattern as context-compaction warnings: surface
facts so the agent can converge, preserve results, prepare a handoff, or request
a revised budget. The script may notify named peers, but does not terminate
agents, reserve funds, or prevent model/tool calls. Existing spending authority
still applies. Automatic hard kills and a core hard-budget controller were
rejected as unnecessary complexity and poor preservation of useful work.

Task-cost runtime code and public fields are removed first. Existing database
columns remain inactive for expand-contract compatibility; a later migration can
drop them after old readers and writers have been retired.

Current owners: [Workflow](../../ava_builtins/skills/practice/ava-workflow/SKILL.md),
[usage reports](../../ava_builtins/skills/coordination/ava-watcher/references/usage.md),
and [task records](../../ava_builtins/plugins/ava_fleet/docs/tasks/tasks.ava.okf.md).
