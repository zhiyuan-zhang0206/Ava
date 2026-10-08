-- Guarded ACTIVE restart acceptance and retained exact source execution facts.
CREATE TABLE native_restart_commands (
    operation_key TEXT PRIMARY KEY CHECK (length(operation_key) BETWEEN 1 AND 128),
    command_id BIGINT NOT NULL UNIQUE CHECK (command_id > 0),
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    work_id UUID NOT NULL UNIQUE,
    target_generation UUID NOT NULL,
    target_owner UUID NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    acceptance JSONB NOT NULL CHECK (jsonb_typeof(acceptance) = 'object'),
    outcome TEXT NOT NULL DEFAULT 'accepted'
        CHECK (outcome IN ('accepted','applied','observed','superseded','uncertain')),
    applied_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    outcome_reason TEXT CHECK (outcome_reason IN ('target_replaced','resurrect','force_terminate',
        'invalid_source_transition','source_command_unavailable')),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (observed_at IS NULL OR applied_at IS NOT NULL),
    CHECK (outcome <> 'observed' OR (applied_at IS NOT NULL AND observed_at IS NOT NULL)),
    CHECK (outcome <> 'applied' OR (applied_at IS NOT NULL AND observed_at IS NULL)),
    CHECK (outcome NOT IN ('accepted','superseded') OR (applied_at IS NULL AND observed_at IS NULL)),
    CHECK (outcome NOT IN ('superseded','uncertain') OR outcome_reason IS NOT NULL)
);
COMMENT ON TABLE native_restart_commands IS
    'Immutable guarded ACTIVE restart acceptance and exact lifecycle source facts. No cleanup FK, TTL, generic Ops claim or execution inferred from status.';

CREATE FUNCTION project_native_restart_source() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    original native_restart_commands%ROWTYPE;
    result_reason TEXT;
    proven_no_effect BOOLEAN := FALSE;
    projected_outcome TEXT;
    projected_reason TEXT;
BEGIN
    SELECT * INTO original FROM native_restart_commands WHERE command_id=NEW.id FOR UPDATE;
    IF NOT FOUND OR original.outcome IN ('observed','superseded') THEN
        RETURN NEW;
    END IF;
    IF NEW.agent_id <> original.agent_id OR NEW.kind <> 'restart'
       OR NEW.target_generation IS DISTINCT FROM original.target_generation
       OR NEW.target_owner IS DISTINCT FROM original.target_owner
       OR NEW.source IS DISTINCT FROM original.request->>'source'
       OR COALESCE(NEW.payload->'config_overlay','null'::jsonb)
          IS DISTINCT FROM original.acceptance->'config_overlay' THEN
        UPDATE native_restart_commands SET outcome='uncertain',outcome_reason='invalid_source_transition'
        WHERE command_id=NEW.id;
        RETURN NEW;
    END IF;
    IF NEW.applied_at IS NOT NULL AND NEW.observed_at IS NOT NULL AND NEW.status='done' THEN
        projected_outcome := 'observed';
    ELSIF NEW.applied_at IS NOT NULL AND NEW.observed_at IS NULL AND NEW.status='claimed' THEN
        projected_outcome := 'applied';
    ELSIF NEW.applied_at IS NULL AND NEW.observed_at IS NULL AND NEW.status='claimed'
          AND original.applied_at IS NULL AND original.outcome='accepted' THEN
        projected_outcome := 'accepted';
    ELSE
        result_reason := NEW.payload->'lifecycle_result'->>'reason';
        IF original.applied_at IS NULL AND NEW.status='done' AND NEW.applied_at IS NULL AND NEW.observed_at IS NULL
           AND NEW.payload->'lifecycle_result'->>'outcome'='superseded' THEN
            IF result_reason='target_replaced' THEN
                SELECT EXISTS(SELECT 1 FROM agents_meta m WHERE m.id=original.agent_id
                    AND m.lifecycle_command_id=NEW.id AND m.runtime_generation IS NOT NULL
                    AND m.runtime_owner IS NOT NULL AND
                    (m.runtime_generation<>original.target_generation OR m.runtime_owner<>original.target_owner))
                INTO proven_no_effect;
            ELSIF result_reason='resurrect' THEN
                SELECT EXISTS(SELECT 1 FROM agents_meta m JOIN inbound_messages r
                    ON r.id=m.last_resurrect_inbound_id AND r.agent_id=m.id AND r.kind='resurrect'
                    WHERE m.id=original.agent_id AND r.id>NEW.id AND
                    to_jsonb(r.id)=NEW.payload->'lifecycle_result'->'resurrect_inbound_id')
                INTO proven_no_effect;
            ELSIF result_reason='force_terminate' THEN
                SELECT EXISTS(SELECT 1 FROM agents_meta m JOIN inbound_messages force
                    ON force.id=m.last_force_terminate_inbound_id AND force.agent_id=m.id AND force.kind='terminate'
                    WHERE m.id=original.agent_id AND force.id>NEW.id)
                INTO proven_no_effect;
            END IF;
        END IF;
        IF proven_no_effect THEN
            projected_outcome := 'superseded';
            projected_reason := result_reason;
        ELSE
            projected_outcome := 'uncertain';
            projected_reason := 'invalid_source_transition';
        END IF;
    END IF;
    UPDATE native_restart_commands SET outcome=projected_outcome,outcome_reason=projected_reason,
        applied_at=CASE WHEN projected_outcome IN ('applied','observed')
            THEN COALESCE(original.applied_at,NEW.applied_at) ELSE original.applied_at END,
        observed_at=CASE WHEN projected_outcome='observed'
            THEN COALESCE(original.observed_at,NEW.observed_at) ELSE original.observed_at END
    WHERE command_id=NEW.id;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_restart_source_projection
AFTER UPDATE ON inbound_messages FOR EACH ROW EXECUTE FUNCTION project_native_restart_source();
