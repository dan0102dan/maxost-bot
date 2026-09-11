-- Existing text cards stay readable; new compatible polls use native Telegram UI.
ALTER TABLE content_cards ADD COLUMN tg_poll_id TEXT;
ALTER TABLE content_cards ADD COLUMN poll_signature TEXT;
ALTER TABLE content_cards ADD COLUMN poll_closed BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX content_cards_native_poll ON content_cards(tg_poll_id)
    WHERE tg_poll_id IS NOT NULL;
