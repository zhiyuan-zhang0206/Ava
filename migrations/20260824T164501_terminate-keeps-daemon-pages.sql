CREATE OR REPLACE FUNCTION cascade_close_agent_pages() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated' THEN
        UPDATE agent_pages SET closed_at = now()
        WHERE agent_id = NEW.id AND closed_at IS NULL AND serve_dir IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
