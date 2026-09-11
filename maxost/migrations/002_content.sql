-- Forward-only upgrade; existing queues and topic mappings remain valid.
ALTER TABLE dialogs ADD COLUMN chat_kind TEXT NOT NULL DEFAULT 'DIALOG';
ALTER TABLE accounts ADD COLUMN reauth_notified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE jobs DROP CONSTRAINT jobs_action_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_action_check CHECK
    (action IN ('send','edit','delete','reaction','react','vote','poll_refresh'));
ALTER TABLE message_links DROP CONSTRAINT message_links_pkey;
ALTER TABLE message_links ADD COLUMN id BIGSERIAL PRIMARY KEY;
ALTER TABLE message_links ADD COLUMN source_key TEXT NOT NULL DEFAULT '';
ALTER TABLE message_links ADD COLUMN album_id TEXT;
ALTER TABLE message_links ADD COLUMN media_tag TEXT;
UPDATE message_links SET source_key = CASE WHEN origin='max' THEN max_message_id ELSE tg_message_id::text END;
CREATE UNIQUE INDEX links_source_part ON message_links(dialog_id,origin,source_key,part,tg_message_id);
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
CREATE TABLE content_cards (
    id BIGSERIAL PRIMARY KEY,
    dialog_id UUID NOT NULL,
    owner BIGINT NOT NULL,
    max_message_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('poll','reactions')),
    tg_message_id BIGINT,
    data BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(dialog_id,owner) REFERENCES dialogs(id,owner) ON DELETE CASCADE,
    UNIQUE(dialog_id,kind,max_message_id)
);
