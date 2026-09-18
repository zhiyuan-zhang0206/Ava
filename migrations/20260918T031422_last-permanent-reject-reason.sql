-- The reason class of the current consecutive permanent-reject streak (the
-- recovery circuit breaker, task #3617): written together with
-- permanent_reject_streak by shared/recovery_breaker.record_permanent_reject_turn
-- (the agent/_runloop._circuit_reason value: 'billing' / 'auth' / ...), reset
-- with the streak by the completed-turn UPDATE. The billing batch-recovery
-- entry (task #3919) reads it to whitelist billing-class halts only.
ALTER TABLE agents_meta ADD COLUMN last_permanent_reject_reason TEXT;

COMMENT ON COLUMN agents_meta.last_permanent_reject_reason IS
    'The reason class of the current consecutive permanent-reject streak '
    '(the _circuit_reason value of the latest permanent provider rejection: '
    '''billing'' for HTTP 402). Written with the streak increment and cleared '
    'with it by the completed-turn UPDATE. The billing batch-recovery entry '
    'reads ''billing'' here (task #3919). NULL = no permanent rejection on '
    'the current streak; never backfilled by guess.';
