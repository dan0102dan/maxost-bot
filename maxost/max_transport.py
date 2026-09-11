"""Version-pinned MAX wire adapter for explicit entities and attachment edits.

PyMax's public send/edit methods parse Markdown. We deliberately use the same
payload models and RPCs as its MessageService instead of changing literal text.
See docs/compatibility.md for the exact upstream revision.
"""
from __future__ import annotations

import asyncio

from .errors import Rejected, Uncertain
from .formatting import to_max
from .max_client import SessionRevoked, is_revoked


async def prepare(client, media, payload):
    from pymax.types.domain.attachments.poll import Poll
    items = []
    notes = []
    for attachment in payload.get('attachments', []):
        if attachment['kind'] == 'poll':
            data = attachment['poll']
            items.append(Poll(title=data['title'], answers=data['answers'], settings=data['settings']))
        else:
            items.append(await media.for_max(attachment))
            if attachment['kind'] == 'sticker':
                notes.append('Стикер Telegram: изображение/файл, не элемент набора MAX.')
    return await client._app.api.messages._upload_attachments(items), notes


async def transmit(client, chat_id, part, attachments, reply=None, message_id=None):
    from pymax.api.binding import bind_api_model
    from pymax.api.messages.payloads import EditMessagePayload, ReplyLink, SendMessagePayload, SendMessagePayloadMessage
    from pymax.api.response import require_payload_model, require_payload_item_model
    from pymax.api.messages.enums import MessagePayloadKey
    from pymax.exceptions import ApiError
    from pymax.protocol import Opcode
    from pymax.types.domain import Message

    elements, notes = to_max(part['text'], part.get('entities', []))
    text = part['text']
    if notes:
        text += '\n[Формат Telegram без аналога MAX: ' + ', '.join(notes) + ']'
    app = client._app
    if message_id is None:
        frame = SendMessagePayload(chat_id=chat_id, message=SendMessagePayloadMessage(
            text=text or None, cid=app.api.messages._next_cid(), elements=elements,
            attaches=attachments, link=ReplyLink(message_id=int(reply)) if reply else None), notify=True)
        pending = getattr(client, '_maxost_sent_cids', set())
        pending.add(frame.message.cid)
        client._maxost_sent_cids = set(sorted(pending)[-2048:])
        opcode = Opcode.MSG_SEND
    else:
        frame = EditMessagePayload(chat_id=chat_id, message_id=int(message_id),
                                   text=text or None, elements=elements, attachments=attachments)
        opcode = Opcode.MSG_EDIT
    try:
        try:
            response = await app.invoke(opcode, frame.to_payload())
        except ApiError as exc:
            if exc.error != 'attachment.not.ready':
                raise
            # A definitive rejection is safe to retry after MAX completes processing.
            # Reuse the same cid/payload, never generate a second logical message.
            await app.api.messages._process_attachment_error(attachments)
            response = await app.invoke(opcode, frame.to_payload())
        message = bind_api_model(app, require_payload_model(response, Message) if message_id is None else require_payload_item_model(response, MessagePayloadKey.MESSAGE, Message))
        polls = [a.model_dump(mode='json', by_alias=False) for a in message.attaches
                 if getattr(getattr(a, 'type', None), 'value', getattr(a, 'type', None)) == 'POLL']
        return {'id': message.id, 'polls': polls}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if is_revoked(exc):
            raise SessionRevoked() from None
        if type(exc).__name__ == 'ApiError':
            raise Rejected('MAX отклонил сообщение/редактирование. Проверьте права в группе или канале.') from None
        raise Uncertain('Результат изменения MAX неизвестен. Проверьте переписку перед повтором.') from None
