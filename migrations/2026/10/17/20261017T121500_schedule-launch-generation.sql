ALTER TABLE schedules ADD COLUMN IF NOT EXISTS launch_generation TEXT;
COMMENT ON COLUMN schedules.launch_generation IS
    'Session-host generation (the pty-sessions service epoch) the current execution attempt was launched into; NULL when the attempt predates this column or the host could not name itself. A live epoch that differs from this value means the host was replaced and the attempt died with it, which authorizes one automatic relaunch (services/wake/schedule_manager/docs/host-replacement.meiva.okf.md#host-replacement-restart).';
