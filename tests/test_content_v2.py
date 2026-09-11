import copy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost.bridge import Bridge, normalize_max
from maxost.content import can_publish, combine_album, from_telegram, supported_chat
from maxost.delivery import Delivery, operations, tag, tg_units
from maxost.errors import Rejected
from maxost.formatting import chunks, from_max, to_max, utf16
from maxost.interactions import Interactions, fingerprint, poll_view, reaction_counts
from maxost.max_client import Connection, MaxHub, NoInteractiveLogin, SessionRevoked


def test_groups_and_channels_not_conflated_with_people():
    assert all(supported_chat(NS(type=t)) for t in ('DIALOG', 'CHAT', 'CHANNEL'))
    assert not supported_chat(NS(type='UNKNOWN'))
    assert not can_publish(NS(type='CHANNEL', owner=2, admins=[], admin_participants={}), 1)
    assert can_publish(NS(type='CHANNEL', owner=1), 1)
    assert can_publish(NS(type='CHANNEL', owner=2, admins=[1]), 1)
    assert can_publish(NS(type='CHAT'), 1)


def test_unknown_attachments_are_explicit():
    p = normalize_max(NS(text='', attaches=[NS(type='FUTURE_ATTACHMENT')], ttl=False))[0]
    assert 'FUTURE_ATTACHMENT' in p['text']
    assert not p['attachments']


def test_entities_preserve_literals_and_utf16_offsets():
    text = '😀 **literal** café\nкод'
    entities = [{'type': 'bold', 'offset': 3, 'length': 11}, {'type': 'italic', 'offset': 5, 'length': 7},
                {'type': 'text_link', 'offset': 15, 'length': 4, 'url': 'https://example.com/a_(b)'}]
    wire, notes = to_max(text, entities)
    assert not notes
    restored, notes = from_max(text, wire)
    assert restored == entities
    pieces = chunks(text, entities, limit=8)
    assert ''.join(p['text'] for p in pieces) == text
    for part in pieces:
        assert utf16(part['text']) <= 8
        assert all(e['offset'] + e['length'] <= utf16(part['text']) for e in part['entities'])


def test_pymax_extra_from_alias_is_not_lost():
    element = NS(type='STRONG', from_=None, length=2, **{'from': 0})
    assert from_max('ok', [element])[0] == [{'type': 'bold', 'offset': 0, 'length': 2}]


def test_unsupported_format_is_reported_not_silently_reparsed():
    wire, notes = to_max('😀', [{'type': 'custom_emoji', 'offset': 0, 'length': 2, 'custom_emoji_id': 'x'}])
    assert wire == [] and notes == ['custom_emoji']
    assert from_max('hello', [{'type': 'FUTURE_STYLE', 'from': 0, 'length': 5}])[1] == ['FUTURE_STYLE']


def photo(tid, group='g'):
    return from_telegram({'message_id': tid, 'caption': f'Фото {tid}', 'media_group_id': group,
        'photo': [{'file_id': str(tid), 'file_size': 1}], 'caption_entities': [{'type': 'bold', 'offset': 0, 'length': 4}]}, 1024)[0]


def test_album_combines_members_and_caption_entities():
    p = combine_album([photo(2), photo(1)])
    assert p['tg_ids'] == [1, 2]
    assert len(p['attachments']) == 2
    assert p['text'] == 'Фото 1\n\nФото 2'
    assert p['entities'][1]['offset'] == utf16('Фото 1\n\n')
    with pytest.raises(Rejected):
        combine_album([photo(i) for i in range(11)])


def test_caption_stays_with_media_and_real_album_plan():
    p = {'text': 'caption', 'entities': [{'type': 'bold', 'offset': 0, 'length': 7}],
         'attachments': [{'kind': 'photo', 'index': 0}, {'kind': 'video', 'index': 1}]}
    units = tg_units(p)
    assert units[0]['text'] == 'caption' and units[1]['text'] == ''
    assert len(operations(units)) == 1
    assert len(operations(units)[0][1]) == 2


class FakeDB:
    def __init__(self, links=()):
        self.steps, self.rows, self.deleted, self.pairs = {}, list(links), [], []
    async def saved_step(self, job, step):
        return copy.deepcopy(self.steps.get((job['id'], step)))
    async def save_step(self, job, step, result):
        self.steps[(job['id'], step)] = copy.deepcopy(result)
    async def mark_sending(self, job):
        pass
    async def source_links(self, d, origin, source_key):
        return self.rows
    async def links(self, d, source, mid):
        return []
    async def save_links(self, job, pairs):
        self.pairs.extend(copy.deepcopy(pairs))
    async def remove_link(self, d, lid):
        self.deleted.append(lid)


class FakeTG:
    def __init__(self):
        self.calls, self.seq = [], 100
    async def pace(self, owner):
        pass
    async def call(self, method, params, files=None):
        self.calls.append((method, copy.deepcopy(params), files))
        self.seq += 1
        if method == 'sendMediaGroup':
            return [{'message_id': self.seq + n, 'media_group_id': 'album'} for n in range(len(params['media']))]
        return {'message_id': params.get('message_id', self.seq)}


def setup(links=(), direction='tg', action='edit'):
    db, tg = FakeDB(links), FakeTG()
    media = NS(for_telegram=AsyncMock(return_value=('photo', 'a.jpg', b'PHOTO')))
    b = NS(db=db, tg=tg, media=media, topic=AsyncMock(return_value=7))
    delivery = Delivery(b)
    d = {'id': 'd', 'owner': 1, 'max_chat_id': 2, 'tg_thread_id': 7}
    j = {'id': 10, 'owner': 1, 'dialog_id': 'd', 'source_id': '42', 'direction': direction, 'action': action, 'part': 0}
    return delivery, db, tg, NS(client=NS()), d, j


def link(n, kind='text', **extras):
    return {'id': n + 1, 'tg_message_id': n + 20, 'max_message_id': '42', 'part': n, 'kind': kind, 'origin': 'max', 'album_id': None, 'media_tag': None, **extras}


@pytest.mark.asyncio
async def test_growing_text_edits_existing_and_appends_only_tail():
    delivery, db, tg, entry, d, job = setup([link(0)])
    await delivery.change(entry, d, job, {'parts': [{'text': 'a' * 3600}]})
    assert [x[0] for x in tg.calls] == ['editMessageText', 'sendMessage']
    assert db.pairs[0]['tg'] == 20
    assert not db.deleted


@pytest.mark.asyncio
async def test_shrinking_text_deletes_surplus_parts():
    delivery, db, tg, entry, d, job = setup([link(0), link(1)])
    await delivery.change(entry, d, job, {'parts': [{'text': 'short'}]})
    assert [x[0] for x in tg.calls] == ['editMessageText', 'deleteMessage']
    assert db.deleted == [2]


@pytest.mark.asyncio
async def test_photo_replacement_uses_edit_media_without_new_message():
    delivery, db, tg, entry, d, job = setup([link(0, 'photo')])
    await delivery.change(entry, d, job, {'parts': [{'text': 'caption', 'attachments': [{'kind': 'photo', 'index': 0, 'identity': 'new'}]}]})
    assert [x[0] for x in tg.calls] == ['editMessageMedia']
    assert tg.calls[0][1]['message_id'] == 20
    assert tg.calls[0][1]['media']['caption'] == 'caption'


@pytest.mark.asyncio
async def test_caption_only_edit_avoids_download():
    a = {'kind': 'photo', 'index': 0, 'identity': 'same'}
    delivery, db, tg, entry, d, job = setup([link(0, 'photo', media_tag=tag(a))])
    await delivery.change(entry, d, job, {'parts': [{'text': 'new caption', 'attachments': [a]}]})
    assert [x[0] for x in tg.calls] == ['editMessageCaption']
    delivery.media.for_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_sent_as_single_api_operation_and_all_ids_mapped():
    delivery, db, tg, entry, d, job = setup(action='send')
    p = {'text': '', 'attachments': [{'kind': 'photo', 'index': i} for i in range(2)]}
    await delivery.send(entry, d, job, p)
    assert [x[0] for x in tg.calls] == ['sendMediaGroup']
    assert len(db.pairs) == 2 and all(p['max'] == '42' for p in db.pairs)
    await delivery.send(entry, d, job, p)
    assert len(tg.calls) == 1  # A retried job reuses its confirmed response.


@pytest.mark.asyncio
async def test_same_size_album_edits_members_in_place():
    delivery, db, tg, entry, d, job = setup([link(0, 'photo', album_id='a'), link(1, 'photo', album_id='a')])
    await delivery.change(entry, d, job, {'parts': [{'text': '', 'attachments': [{'kind': 'photo', 'index': i} for i in range(2)]}]})
    assert [x[0] for x in tg.calls] == ['editMessageMedia', 'editMessageMedia']
    assert not db.deleted


@pytest.mark.asyncio
async def test_changed_album_size_rebuilds_then_removes_old():
    delivery, db, tg, entry, d, job = setup([link(0, 'photo', album_id='a'), link(1, 'photo', album_id='a')])
    await delivery.change(entry, d, job, {'parts': [{'text': '', 'attachments': [{'kind': 'photo', 'index': i} for i in range(3)]}]})
    assert [x[0] for x in tg.calls] == ['sendMediaGroup', 'deleteMessage', 'deleteMessage']
    assert len(db.pairs) == 3


@pytest.mark.asyncio
async def test_telegram_album_is_one_max_message(monkeypatch):
    delivery, db, tg, entry, d, job = setup(direction='max', action='send')
    from maxost import max_transport
    prepare = AsyncMock(return_value=(['uploaded1', 'uploaded2'], []))
    transmit = AsyncMock(return_value={'id': 99, 'polls': []})
    monkeypatch.setattr(max_transport, 'prepare', prepare)
    monkeypatch.setattr(max_transport, 'transmit', transmit)
    await delivery.send(entry, d, job, combine_album([photo(1), photo(2)]))
    assert transmit.await_count == 1
    assert transmit.await_args.args[3] == ['uploaded1', 'uploaded2']
    assert {p['tg'] for p in db.pairs} == {1, 2}
    assert {p['max'] for p in db.pairs} == {99}


def sample_poll():
    return {'poll_id': 99, 'title': 'Выбор?', 'settings': 7,
            'answers': [{'answer_id': 10, 'text': 'A'}, {'answer_id': 20, 'text': 'B'}],
            'state': {'total': 9, 'result': [{'answer_id': 10, 'vote_count': 9}]}}


def test_poll_card_shows_real_counts_and_versioned_buttons():
    poll = sample_poll()
    text, keyboard = poll_view(1, poll)
    assert 'Всего голосов в MAX: 9' in text
    assert 'A — 9' in text
    assert all(len(b['callback_data'].encode()) <= 64 for r in keyboard['inline_keyboard'] for b in r)
    changed = copy.deepcopy(poll)
    changed['answers'].reverse()
    assert fingerprint(changed) != fingerprint(poll)
    poll['settings'] |= 8
    assert all(':v:' not in b['callback_data'] for r in poll_view(1, poll)[1]['inline_keyboard'] for b in r)


def test_own_reaction_does_not_echo():
    info = NS(your_reaction='👍', counters=[NS(reaction='👍', count=1), NS(reaction='❤️', count=3)])
    assert reaction_counts(info) == [{'reaction': '❤️', 'count': 3}]


@pytest.mark.asyncio
async def test_callback_cannot_use_foreign_card():
    db = NS(pool=NS(fetchrow=AsyncMock(return_value=None)))
    interactions = Interactions(NS(db=db, tg=NS()))
    q = {'id': 'q', 'from': {'id': 1}, 'message': {'message_id': 2, 'chat': {'id': 1, 'type': 'private'}}, 'data': 'c:3:reaction:r:0'}
    with pytest.raises(Rejected):
        await interactions.callback(q)
    assert db.pool.fetchrow.await_args.args[1:] == (3, 1)


@pytest.mark.asyncio
async def test_session_revoked_closes_connection_without_sms():
    db = NS(pool=NS(execute=AsyncMock()))
    hub = MaxHub(db, NS())
    entry = Connection({'id': 'account', 'owner': 1}, client=NS(close=AsyncMock()))
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
    b = Bridge(db, NS(), NS(), NS(), NS())
    client = NS(get_chat=AsyncMock(), _maxost_sent_cids={123})
    entry = NS(account={'id': 'a', 'owner': 1, 'max_user_id': 2, 'since_ms': 10000}, closing=False, client=client)
    await b.from_max(entry, 'send', NS(chat_id=3, sender=2, time=20000))
    await b.from_max(entry, 'send', NS(chat_id=3, sender=4, time=1))
    await b.from_max(entry, 'send', NS(chat_id=3, sender=None, cid=123, time=20000))
    client.get_chat.assert_not_awaited()
