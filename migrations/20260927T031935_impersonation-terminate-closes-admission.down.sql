-- Restore the termination trigger body that ends leases without closing
-- manifest admission (terminal replay then keeps such leases pending).
CREATE OR REPLACE FUNCTION revoke_terminated_impersonation() RETURNS trigger AS $$
BEGIN
    UPDATE agent_impersonations SET status='expired', ended_at=clock_timestamp()
    WHERE agent_id=NEW.id AND status IN ('requested','accepted','active');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
