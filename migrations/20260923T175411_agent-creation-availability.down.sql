ALTER TABLE machine_probe DROP COLUMN agent_host_online;
ALTER TABLE agents_meta
    DROP CONSTRAINT agents_meta_admission_observation_pair_check,
    DROP COLUMN last_admission_outcome,
    DROP COLUMN last_admission_at;
