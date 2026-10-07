ALTER TABLE agents_meta
    ADD COLUMN last_admission_outcome TEXT
        CHECK (last_admission_outcome IN
            ('admitted', 'maintenance_hold', 'publication_deferred',
             'resource_fence', 'admission_guard_refused')),
    ADD COLUMN last_admission_at TIMESTAMPTZ,
    ADD CONSTRAINT agents_meta_admission_observation_pair_check
        CHECK ((last_admission_outcome IS NULL) = (last_admission_at IS NULL));

ALTER TABLE machine_probe ADD COLUMN agent_host_online BOOLEAN;
