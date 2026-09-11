-- Persist which PyMax runtime owns the encrypted session.
-- Existing SMS/mobile sessions remain mobile; QR sessions use WebClient after restart.
ALTER TABLE accounts
    ADD COLUMN client_kind TEXT NOT NULL DEFAULT 'mobile';

ALTER TABLE accounts
    ADD CONSTRAINT accounts_client_kind_check
    CHECK (client_kind IN ('mobile','web'));
