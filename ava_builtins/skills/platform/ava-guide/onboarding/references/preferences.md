# Initial preference questions

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Question list

Wording below is a floor, not a script — ask naturally, but capture every
group.

### Language

- "What language should I use when talking to you?"
- Follow up only if ambiguous: "Same for reports and pages, or different?"
- Record: `type/user` (an attribute of the person); add `type/feedback` if
  the user states it as a rule ("always Chinese, including reports").

### Notification channel

- "When you are not in this dialog, how should I reach you — a notice in
  the queue, or something else?"
- "Which decisions should I wait for your OK on instead of deciding
  myself?" (Expect: irreversible actions, outward-facing actions, spending
  real money, contacting real people.)
- Record: `type/feedback` — it is how you work, with the reason the user
  gave.

### Timezone

- "Which timezone should schedules and reminders use?" Offer to derive it
  from their location if they are unsure.
- Record: `type/user`; add `type/feedback` if it changes how you schedule
  (e.g. "never schedule anything before 9 AM local").

### Update rhythm

- "How fast should this cluster take updates — as soon as a release lands,
  on a schedule, or only when you say?"
- "Are there hours when I should avoid maintenance?"
- Record: `type/feedback` (and `type/env` if the window is a machine fact).

### Confirmation gates

- "What should I never do without checking with you first?"
- Record: `type/feedback` — a standing rule, not a one-off.

### Reporting style

- "Do you want finished work as a served page, a short chat summary, or
  both? How often should progress updates arrive while something is
  running?"
- Record: `type/feedback`.
