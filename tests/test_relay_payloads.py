"""The relay never writes conversion diagnostics into someone else's conversation."""
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost import max_transport
from maxost.delivery import Delivery
from maxost.media import Media
from maxost.telegram import TelegramRejected


@pytest.mark.parametrize('flags,name', [
    ({}, 'sticker.webp'), ({'is_animated': True}, 'sticker.tgs'),
    ({'is_video': True}, 'sticker.webm'),
])
@pytest.mark.asyncio
async def test_telegram_sticker_is_original_document_without_conversion(monkeypatch, flags, name):
    def file(**kwargs):
        return NS(kind='file', **kwargs)
    def unexpected(**kwargs):
        raise AssertionError('Sticker must not be transcoded to a different media type')
    monkeypatch.setitem(sys.modules, 'pymax', NS(
        File=file, Photo=unexpected, Video=unexpected, VideoNote=unexpected, Voice=unexpected,
    ))
    raw = b'original sticker bytes'
    tg = NS(download=AsyncMock(return_value=raw))
    result = await Media(tg, 1024).for_max({
        'file_id': 'file', 'kind': 'sticker', 'name': name, **flags,
    })
    assert result.kind == 'file' and result.raw == raw and result.name == name


@pytest.mark.asyncio
async def test_prepare_returns_attachments_only_no_sticker_notes():
    pytest.importorskip('pymax')
    upload = AsyncMock(return_value=['uploaded-file'])
    client = NS(_app=NS(api=NS(messages=NS(_upload_attachments=upload))))
    media = NS(for_max=AsyncMock(return_value='file-object'))
    result = await max_transport.prepare(client, media, {
        'text': '', 'attachments': [{'kind': 'sticker', 'file_id': 'file'}],
    })
    assert result == ['uploaded-file']
    upload.assert_awaited_once_with(['file-object'])


@pytest.mark.parametrize('message_id', [None, '91'])
@pytest.mark.parametrize('entity', [
    {'type': 'custom_emoji', 'offset': 0, 'length': 2, 'custom_emoji_id': 'custom'},
    {'type': 'pre', 'offset': 3, 'length': 4, 'language': 'python'},
])
@pytest.mark.asyncio
async def test_wire_send_and_edit_preserve_original_text_with_unsupported_entities(message_id, entity):
    pytest.importorskip('pymax')
    from pymax.protocol import Opcode
    source = '😀 code\n[Стикер Telegram: это оригинальный текст] **literal**'
    calls = []
    async def invoke(opcode, payload):
        calls.append(payload)
        message = {'id': 91, 'time': 1, 'type': 'USER', 'text': source, 'attaches': []}
        return NS(payload=message if opcode == Opcode.MSG_SEND else {'message': message})
    client = NS(_app=NS(invoke=invoke, api=NS(messages=NS(_next_cid=lambda: 77))))
    await max_transport.transmit(client, 500, {'text': source, 'entities': [entity]}, [], message_id=message_id)
    sent_text = calls[0]['message']['text'] if message_id is None else calls[0]['text']
    assert sent_text == source


@pytest.mark.parametrize('text', ['', 'Моя подпись', '😀' * 2000])
@pytest.mark.asyncio
async def test_delivery_does_not_add_text_to_sticker_or_caption(monkeypatch, text):
    db = NS(saved_step=AsyncMock(return_value=None), mark_sending=AsyncMock(),
            save_step=AsyncMock(), save_links=AsyncMock())
    delivery = Delivery(NS(db=db, tg=NS(), media=NS()))
    prepare = AsyncMock(return_value=['file'])
    transmit = AsyncMock(return_value={'id': 91})
    monkeypatch.setattr(max_transport, 'prepare', prepare)
    monkeypatch.setattr(max_transport, 'transmit', transmit)
    await delivery.send_max(
        NS(client=NS()), {'owner': 101, 'max_chat_id': 500}, {'source_id': '42'},
        {'text': text, 'entities': [], 'attachments': [{'kind': 'sticker'}]}, None,
    )
    assert ''.join(call.args[2]['text'] for call in transmit.await_args_list) == text
    assert transmit.await_args_list[0].args[3] == ['file']
    for call in transmit.await_args_list[1:]:
        assert call.args[3] == []


@pytest.mark.asyncio
async def test_incoming_sticker_document_fallback_keeps_original_caption():
    tg = NS(pace=AsyncMock(), call=AsyncMock(side_effect=[
        TelegramRejected(400, 'STICKER_INVALID'), {'message_id': 72},
    ]))
    media = NS(for_telegram=AsyncMock(return_value=('sticker', 'sticker.tgs', b'original')))
    delivery = Delivery(NS(db=NS(), tg=tg, media=media))
    await delivery.send_tg(NS(client=NS()), {'owner': 101, 'max_chat_id': 500},
        {'source_id': '42'}, [{'kind': 'sticker', 'text': 'Оригинальная подпись',
                             'entities': [], 'attachment': {'index': 0, 'kind': 'sticker'}}], 7, None)
    method, params = tg.call.await_args.args
    assert method == 'sendDocument' and params['caption'] == 'Оригинальная подпись'
    assert tg.call.await_args.kwargs['files']['document'][1] == b'original'
