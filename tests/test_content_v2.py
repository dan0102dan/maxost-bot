import copy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost.bridge import Bridge, normalize_max
from maxost.content import can_publish, combine_album, from_telegram, supported_chat
from maxost.delivery import Delivery, operations, tag, tg_units
from maxost.errors import Rejected
from maxost.formatting import chunks, from_max, to_max, utf16
from maxost.interactions import reaction_counts
from maxost.max_client import Connection, MaxHub, NoInteractiveLogin, SessionRevoked


def test_groups_and_channels_not_conflated_with_people():
    assert all(supported_chat(NS(type=kind)) for kind in ('DIALOG', 'CHAT', 'CHANNEL'))
    assert not supported_chat(NS(type='UNKNOWN'))
    assert not can_publish(
        NS(type='CHANNEL', owner=2, admins=[], admin_participants={}), 1
    )
    assert can_publish(NS(type='CHANNEL', owner=1), 1)
    assert can_publish(NS(type='CHANNEL', owner=2, admins=[1]), 1)
    assert can_publish(NS(type='CHAT'), 1)


def test_unknown_attachments_are_explicit():
    payload = normalize_max(
        NS(text='', attaches=[NS(type='FUTURE_ATTACHMENT')], ttl=False)
    )[0]
    assert 'FUTURE_ATTACHMENT' in payload['text']
    assert not payload['attachments']


def test_entities_preserve_literals_and_utf16_offsets():
    text = '😀 **literal** café\nкод'
    entities = [
        {'type': 'bold', 'offset': 3, 'length': 11},
        {'type': 'italic', 'offset': 5, 'length': 7},
        {
            'type': 'text_link',
            'offset': 15,
            'length': 4,
            'url': 'https://example.com/a_(b)',
        },
    ]
    wire, notes = to_max(text, entities)
    assert not notes
    restored, notes = from_max(text, wire)
    assert restored == entities
    pieces = chunks(text, entities, limit=8)
    assert ''.join(part['text'] for part in pieces) == text
    for part in pieces:
        assert utf16(part['text']) <= 8
        assert all(
            entity['offset'] + entity['length'] <= utf16(part['text'])
            for entity in part['entities']
        )


def test_pymax_extra_from_alias_is_not_lost():
    element = NS(type='STRONG', from_=None, length=2, **{'from': 0})
    assert from_max('ok', [element])[0] == [
        {'type': 'bold', 'offset': 0, 'length': 2}
    ]


def test_unsupported_format_is_reported_not_silently_reparsed():
    wire, notes = to_max(
        '😀',
        [{
            'type': 'custom_emoji',
            'offset': 0,
            'length': 2,
            'custom_emoji_id': 'x',
        }],
    )
    assert wire == [] and notes == ['custom_emoji']
    assert from_max(
        'hello', [{'type': 'FUTURE_STYLE', 'from': 0, 'length': 5}]
    )[1] == ['FUTURE_STYLE']


def photo(message_id, group='g'):
    return from_telegram(
        {
            'message_id': message_id,
            'caption': f'Фото {message_id}',
            'media_group_id': group,
            'photo': [{'file_id': str(message_id), 'file_size': 1}],
            'caption_entities': [
                {'type': 'bold', 'offset': 0, 'length': 4}
            ],
        },
        1024,
    )[0]


def test_album_combines_members_and_caption_entities():
    payload = combine_album([photo(2), photo(1)])
    assert payload['tg_ids'] == [1, 2]
    assert len(payload['attachments']) == 2
    assert payload['text'] == 'Фото 1\n\nФото 2'
    assert payload['entities'][1]['offset'] == utf16('Фото 1\n\n')
    with pytest.raises(Rejected):
        combine_album([photo(index) for index in range(11)])


def test_caption_stays_with_media_and_real_album_plan():
    payload = {
        'text': 'caption',
        'entities': [{'type': 'bold', 'offset': 0, 'length': 7}],
        'attachments': [
            {'kind': 'photo', 'index': 0},
            {'kind': 'video', 'index': 1},
        ],
    }
    units = tg_units(payload)
    assert units[0]['text'] == 'caption' and units[1]['text'] == ''
    assert len(operations(units)) == 1
    assert len(operations(units)[0][1]) == 2


class FakeDB:
    def __init__(self, links=()):
        self.steps = {}
        self.rows = list(links)
        self.deleted = []
        self.pairs = []

    async def saved_step(self, job, step):
        return copy.deepcopy(self.steps.get((job['id'], step)))

    async def save_step(self, job, step, result):
        self.steps[(job['id'], step)] = copy.deepcopy(result)

    async def mark_sending(self, job):
        pass

    async def source_links(self, dialog, origin, source_key):
        return self.rows

    async def links(self, dialog, source, message_id):
        return []

    async def save_links(self, job, pairs):
        self.pairs.extend(copy.deepcopy(pairs))

    async def remove_link(self, dialog, link_id):
        self.deleted.append(link_id)


class FakeTG:
    def __init__(self):
        self.calls = []
        self.seq = 100

    async def pace(self, owner):
        pass

    async def call(self, method, params, files=None):
        self.calls.append((method, copy.deepcopy(params), files))
        self.seq += 1
        if method == 'sendMediaGroup':
            return [
                {
                    'message_id': self.seq + index,
                    'media_group_id': 'album',
                }
                for index in range(len(params['media']))
            ]
        return {'message_id': params.get('message_id', self.seq)}


def setup(links=(), direction='tg', action='edit'):
    db, tg = FakeDB(links), FakeTG()
    media = NS(
        for_telegram=AsyncMock(return_value=('photo', 'a.jpg', b'PHOTO'))
    )
    bridge = NS(db=db, tg=tg, media=media, topic=AsyncMock(return_value=7))
    delivery = Delivery(bridge)
    dialog = {
        'id': 'd',
        'owner': 1,
        'max_chat_id': 2,
        'tg_thread_id': 7,
    }
    job = {
        'id': 10,
        'owner': 1,
        'dialog_id': 'd',
        'source_id': '42',
        'direction': direction,
        'action': action,
        'part': 0,
    }
    return delivery, db, tg, NS(client=NS()), dialog, job


def link(index, kind='text', **extras):
    return {
        'id': index + 1,
        'tg_message_id': index + 20,
        'max_message_id': '42',
        'part': index,
        'kind': kind,
        'origin': 'max',
        'album_id': None,
        'media_tag': None,
        **extras,
    }


@pytest.mark.asyncio
async def test_growing_text_edits_existing_and_appends_only_tail():
    delivery, db, tg, entry, dialog, job = setup([link(0)])
    await delivery.change(
        entry, dialog, job, {'parts': [{'text': 'a' * 3600}]}
    )
    assert [call[0] for call in tg.calls] == ['editMessageText', 'sendMessage']
    assert db.pairs[0]['tg'] == 20
    assert not db.deleted


@pytest.mark.asyncio
async def test_shrinking_text_deletes_surplus_parts():
    delivery, db, tg, entry, dialog, job = setup([link(0), link(1)])
    await delivery.change(entry, dialog, job, {'parts': [{'text': 'short'}]})
    assert [call[0] for call in tg.calls] == [
        'editMessageText',
        'deleteMessage',
    ]
    assert db.deleted == [2]


@pytest.mark.asyncio
async def test_photo_replacement_uses_edit_media_without_new_message():
    delivery, db, tg, entry, dialog, job = setup([link(0, 'photo')])
    await delivery.change(
        entry,
        dialog,
        job,
        {
            'parts': [{
                'text': 'caption',
                'attachments': [
                    {'kind': 'photo', 'index': 0, 'identity': 'new'}
                ],
            }]
        },
    )
    assert [call[0] for call in tg.calls] == ['editMessageMedia']
    assert tg.calls[0][1]['message_id'] == 20
    assert tg.calls[0][1]['media']['caption'] == 'caption'


@pytest.mark.asyncio
async def test_caption_only_edit_avoids_download():
    attachment = {'kind': 'photo', 'index': 0, 'identity': 'same'}
    delivery, db, tg, entry, dialog, job = setup([
        link(0, 'photo', media_tag=tag(attachment))
    ])
    await delivery.change(
        entry,
        dialog,
        job,
        {
            'parts': [{
                'text': 'new caption',
                'attachments': [attachment],
            }]
        },
    )
    assert [call[0] for call in tg.calls] == ['editMessageCaption']
    delivery.media.for_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_sent_as_single_api_operation_and_all_ids_mapped():
    delivery, db, tg, entry, dialog, job = setup(action='send')
    payload = {
        'text': '',
        'attachments': [
            {'kind': 'photo', 'index': index} for index in range(2)
        ],
    }
    await delivery.send(entry, dialog, job, payload)
    assert [call[0] for call in tg.calls] == ['sendMediaGroup']
    assert len(db.pairs) == 2
    assert all(pair['max'] == '42' for pair in db.pairs)
    await delivery.send(entry, dialog, job, payload)
    assert len(tg.calls) == 1


@pytest.mark.asyncio
async def test_same_size_album_edits_members_in_place():
    delivery, db, tg, entry, dialog, job = setup([
        link(0, 'photo', album_id='a'),
        link(1, 'photo', album_id='a'),
    ])
    await delivery.change(
        entry,
        dialog,
        job,
        {
            'parts': [{
                'text': '',
                'attachments': [
                    {'kind': 'photo', 'index': index} for index in range(2)
                ],
            }]
        },
    )
    assert [call[0] for call in tg.calls] == [
        'editMessageMedia',
        'editMessageMedia',
    ]
    assert not db.deleted


@pytest.mark.asyncio
async def test_changed_album_size_rebuilds_then_removes_old():
    delivery, db, tg, entry, dialog, job = setup([
        link(0, 'photo', album_id='a'),
        link(1, 'photo', album_id='a'),
    ])
    await delivery.change(
        entry,
        dialog,
        job,
        {
            'parts': [{
                'text': '',
                'attachments': [
                    {'kind': 'photo', 'index': index} for index in range(3)
                ],
            }]
        },
    )
    assert [call[0] for call in tg.calls] == [
        'sendMediaGroup',
        'deleteMessage',
        'deleteMessage',
    ]
    assert len(db.pairs) == 3


@pytest.mark.asyncio
async def test_telegram_album_is_one_max_message(monkeypatch):
    delivery, db, tg, entry, dialog, job = setup(
        direction='max', action='send'
    )
    from maxost import max_transport

    prepare = AsyncMock(return_value=(['uploaded1', 'uploaded2'], []))
    transmit = AsyncMock(return_value={'id': 99, 'polls': []})
    monkeypatch.setattr(max_transport, 'prepare', prepare)
    monkeypatch.setattr(max_transport, 'transmit', transmit)
    await delivery.send(
        entry,
        dialog,
        job,
        combine_album([photo(1), photo(2)]),
    )
    assert transmit.await_count == 1
    assert transmit.await_args.args[3] == ['uploaded1', 'uploaded2']
    assert {pair['tg'] for pair in db.pairs} == {1, 2}
    assert {pair['max'] for pair in db.pairs} == {99}


def test_own_reaction_does_not_echo():
    info = NS(
        your_reaction='👍',
        counters=[
            NS(reaction='👍', count=1),
            NS(reaction='❤️', count=3),
        ],
    )
    assert reaction_counts(info) == [{'reaction': '❤️', 'count': 3}]


@pytest.mark.asyncio
async def test_session_revoked_closes_connection_without_sms():
    db = NS(pool=NS(execute=AsyncMock()))
    hub = MaxHub(db, NS())
    entry = Connection(
        {'id': 'account', 'owner': 1},
        client=NS(close=AsyncMock()),
    )
    entry.ready.set()
    await hub.reauth(entry)
    assert entry.closing and not entry.ready.is_set()
    entry.client.close.assert_awaited_once()
    assert 'session_cipher=NULL' in db.pool.execute.await_args.args[0]
    with pytest.raises(SessionRevoked):
        await NoInteractiveLogin().authenticate(NS())


@pytest.mark.asyncio
async def test_self_message_and_preconnection_history_still_suppressed():
    db = NS(pool=NS(fetchrow=AsyncMock(), execute=AsyncMock()))
    bridge = Bridge(db, NS(), NS(), NS(), NS())
    client = NS(get_chat=AsyncMock(), _maxost_sent_cids={123})
    entry = NS(
        account={
            'id': 'a',
            'owner': 1,
            'max_user_id': 2,
            'since_ms': 10000,
        },
        closing=False,
        client=client,
    )
    await bridge.from_max(
        entry, 'send', NS(chat_id=3, sender=2, time=20000)
    )
    await bridge.from_max(
        entry, 'send', NS(chat_id=3, sender=4, time=1)
    )
    await bridge.from_max(
        entry,
        'send',
        NS(chat_id=3, sender=None, cid=123, time=20000),
    )
    client.get_chat.assert_not_awaited()
