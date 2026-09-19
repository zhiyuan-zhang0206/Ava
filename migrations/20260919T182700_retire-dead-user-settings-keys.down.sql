-- Best-effort reverse of retire-dead-user-settings-keys: re-insert the rows
-- with the values observed at migration time on the single-user deployment
-- (task #4086). The keys are inert in every release that ran with them, so
-- there is no functional state to restore beyond the rows themselves; ON
-- CONFLICT keeps a re-run safe.

INSERT INTO user_settings (key, value) VALUES
    ('display.sidebar_width', '442'::jsonb),
    ('display.show_machine_name', 'true'::jsonb),
    ('display.collapse_agent_runs', 'true'::jsonb),
    ('display.show_reasoning', 'true'::jsonb),
    ('display.show_code', 'true'::jsonb),
    ('display.show_output', 'true'::jsonb),
    ('display.task_force_params', '{"repulsion": 25000, "alphaDecay": 0.2, "nodeSizeMax": 26, "nodeSizeMin": 18, "zoomPadding": 40, "centerForceX": 0.02, "centerForceY": 0.02, "linkDistance": 170, "linkStrength": 0.25, "zoomFitRatio": 1, "centerStrength": 0.9, "collidePadding": 8}'::jsonb)
ON CONFLICT (key) DO NOTHING;
