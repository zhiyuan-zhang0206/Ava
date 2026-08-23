# Runner-batch audit fixes

The R-3/R-4/R-6/R-7 audit residues were closed in one bounded change set.

- Graceful termination now classifies a stored PID by agent identity. Foreign
  and gone PIDs reconcile the stale row without clearing a session or signalling
  a process; owned and unreadable PIDs retain the inbound path.
- Exec request and result envelopes record their final size and serialization
  time in the event stream. Over-ceiling errors now direct the agent to compact
  the conversation, and the child initializes logging and signal handlers before
  decoding the request.
- Heartbeat selection treats a pause as a floor on the normal idle-clock due
  time, so a real turn during the pause still receives its normal idle interval
  after the window ends.

Each changed behavior has regression coverage, including red-before-green
verification of the audited failure modes.
