CREATE TABLE users (
    id BIGINT PRIMARY KEY CHECK (id > 0),
    consent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    owner BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    max_user_id BIGINT UNIQUE,
    phone_hash TEXT NOT NULL UNIQUE,
    phone_cipher BYTEA NOT NULL,
    session_cipher BYTEA,
    client_kind TEXT NOT NULL DEFAULT 'mobile'
        CHECK (client_kind IN ('mobile','web')),
    status TEXT NOT NULL DEFAULT 'authorizing'
        CHECK (status IN ('authorizing','connected','offline','reauth','paused')),
    reauth_notified BOOLEAN NOT NULL DEFAULT false,
    since_ms BIGINT NOT NULL,
    history_ms BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, owner)
);

CREATE TABLE dialogs (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL,
    owner BIGINT NOT NULL,
    max_chat_id BIGINT NOT NULL,
    peer_id BIGINT,
    chat_kind TEXT NOT NULL DEFAULT 'DIALOG'
        CHECK (chat_kind IN ('DIALOG','CHAT','CHANNEL')),
    title_cipher BYTEA NOT NULL,
    tg_thread_id BIGINT,
    topic_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (topic_state IN ('pending','creating','ready','unknown')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (account_id, owner) REFERENCES accounts(id, owner) ON DELETE CASCADE,
    UNIQUE (account_id, max_chat_id),
    UNIQUE (owner, tg_thread_id),
    UNIQUE (id, owner)
);

CREATE TABLE jobs (
    id BIGSERIAL PRIMARY KEY,
    owner BIGINT NOT NULL,
    dialog_id UUID NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('tg','max')),
    action TEXT NOT NULL
        CHECK (action IN ('send','edit','delete','reaction','react','vote','poll_refresh')),
    source_id TEXT NOT NULL,
    revision TEXT NOT NULL DEFAULT '',
    part INTEGER NOT NULL DEFAULT 0 CHECK (part >= 0),
    payload BYTEA,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','sending','sent','failed','unknown','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error TEXT,
    notified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (dialog_id, owner) REFERENCES dialogs(id, owner) ON DELETE CASCADE,
    UNIQUE (dialog_id, direction, action, source_id, revision, part),
    UNIQUE (id, owner)
);
CREATE INDEX jobs_ready ON jobs(available_at, id) WHERE status = 'pending';
CREATE INDEX jobs_dialog_order ON jobs(dialog_id, direction, id)
    WHERE status NOT IN ('sent','skipped');

CREATE TABLE message_links (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    owner BIGINT NOT NULL,
    dialog_id UUID NOT NULL,
    tg_message_id BIGINT NOT NULL,
    max_message_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    origin TEXT NOT NULL CHECK (origin IN ('tg','max')),
    part INTEGER NOT NULL DEFAULT 0,
    source_key TEXT NOT NULL,
    album_id TEXT,
    media_tag TEXT,
    FOREIGN KEY (job_id, owner) REFERENCES jobs(id, owner) ON DELETE CASCADE,
    FOREIGN KEY (dialog_id, owner) REFERENCES dialogs(id, owner) ON DELETE CASCADE
);
CREATE UNIQUE INDEX links_source_part
    ON message_links(dialog_id,origin,source_key,part,tg_message_id);
CREATE INDEX links_tg ON message_links(owner, dialog_id, tg_message_id, part);
CREATE INDEX links_max ON message_links(owner, dialog_id, max_message_id, part);

CREATE TABLE delivery_steps (
    job_id BIGINT NOT NULL,
    owner BIGINT NOT NULL,
    step TEXT NOT NULL,
    result BYTEA NOT NULL,
    PRIMARY KEY(job_id,step),
    FOREIGN KEY(job_id,owner) REFERENCES jobs(id,owner) ON DELETE CASCADE
);

CREATE TABLE albums (
    dialog_id UUID NOT NULL,
    owner BIGINT NOT NULL,
    group_id TEXT NOT NULL,
    body BYTEA NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    flushed_generation BIGINT NOT NULL DEFAULT 0,
    ready_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(dialog_id,group_id),
    FOREIGN KEY(dialog_id,owner) REFERENCES dialogs(id,owner) ON DELETE CASCADE
);

CREATE TABLE poll_mirrors (
    id BIGSERIAL PRIMARY KEY,
    dialog_id UUID NOT NULL,
    owner BIGINT NOT NULL,
    max_message_id TEXT NOT NULL,
    tg_message_id BIGINT,
    tg_poll_id TEXT UNIQUE,
    signature TEXT,
    closed BOOLEAN NOT NULL DEFAULT false,
    data BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(dialog_id,owner) REFERENCES dialogs(id,owner) ON DELETE CASCADE,
    UNIQUE(dialog_id,max_message_id)
);

CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);

CREATE TABLE auth_attempts (
    id BIGSERIAL PRIMARY KEY,
    owner BIGINT NOT NULL,
    phone_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX auth_owner_time ON auth_attempts(owner, created_at);
CREATE INDEX auth_phone_time ON auth_attempts(phone_hash, created_at);

CREATE FUNCTION maxost_guard_dialog_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM dialogs WHERE id=NEW.dialog_id AND owner=NEW.owner FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Content dialog ownership mismatch' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER albums_owner_guard BEFORE INSERT OR UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_dialog_owner();
CREATE TRIGGER message_links_dialog_owner_guard BEFORE INSERT OR UPDATE ON message_links
    FOR EACH ROW EXECUTE FUNCTION maxost_guard_dialog_owner();
CREATE TRIGGER poll_mirrors_owner_guard BEFORE INSERT OR UPDATE ON poll_mirrors
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
