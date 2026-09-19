-- Dead user_settings keys retired (task #4086, batch 2, audit item
-- w1-dead-setting-keys). None of these keys has a reader in the frontend:
-- display.sidebar_width (an ignored legacy pixel value — the homepage split is
-- library-owned device-local state), display.show_machine_name /
-- display.collapse_agent_runs / display.show_reasoning / display.show_code /
-- display.show_output (their readers were removed earlier), and
-- display.task_force_params (superseded by display.task_force_params.v2 in the
-- #739 key bump — the pre-v2 tuning was shed with the node-geometry change).
-- The frontend defaults entry for display.sidebar_width is removed in the same
-- release. Idempotent: deletes only rows that still exist.

DELETE FROM user_settings
WHERE key IN (
    'display.sidebar_width',
    'display.show_machine_name',
    'display.collapse_agent_runs',
    'display.show_reasoning',
    'display.show_code',
    'display.show_output',
    'display.task_force_params'
);
