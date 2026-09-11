-- BEFORE INSERT also runs before an ON CONFLICT UPDATE/DO NOTHING branch.
-- A composite FK alone cannot validate the discarded EXCLUDED.owner value.
-- Lock the parent key and reject forged owners BEFORE conflict resolution.
CREATE FUNCTION maxost_guard_dialog_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM dialogs WHERE id=NEW.dialog_id AND owner=NEW.owner FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Content dialog ownership mismatch' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER content_cards_owner_guard BEFORE INSERT OR UPDATE ON content_cards
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_dialog_owner();
CREATE TRIGGER albums_owner_guard BEFORE INSERT OR UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_dialog_owner();
CREATE TRIGGER message_links_dialog_owner_guard BEFORE INSERT OR UPDATE ON message_links
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_dialog_owner();

CREATE FUNCTION maxost_guard_job_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM jobs WHERE id=NEW.job_id AND owner=NEW.owner FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Content job ownership mismatch' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER delivery_steps_owner_guard BEFORE INSERT OR UPDATE ON delivery_steps
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_job_owner();
CREATE TRIGGER message_links_job_owner_guard BEFORE INSERT OR UPDATE ON message_links
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_job_owner();
