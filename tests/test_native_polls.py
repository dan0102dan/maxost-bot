"""Native Bot API payloads and ownership, without contacting Telegram or MAX."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost.errors import Rejected
from maxost.native_polls import NativePolls, native_parameters, poll_signature, results_text
from maxost.native_reactions import mirror_reactions
from maxost.telegram import TelegramRejected


def poll(flags=5):
    return {'poll_id': 500, 'title': 'Куда?', 'settings': flags,
            'answers': [{'answer_id': 20, 'text': 'Парк'}, {'answer_id': 80, 'text': 'Кино'}],
            'state': {'total': 9, 'result': [{'answer_id': 20, 'vote_count': 7}, {'answer_id': 80, 'vote_count': 2}]}}


class Storage:
    def __init__(self):
        self.card = None
        self.dialog = {'id': 'd', 'owner': 101, 'tg_thread_id': 7, 'max_chat_id': 900}
        self.pool = NS(fetchrow=AsyncMock(side_effect=self.fetchrow), execute=AsyncMock(side_effect=self.execute))
        self.enqueue = AsyncMock()

    async def fetchrow(self, sql, *args):
        if 'FROM dialogs' in sql:
            return dict(self.dialog) if args == ('d', 101) else None
        c = self.card
        if not c:
            return None
        if 'owner=$1 AND tg_poll_id=$2' in sql:
            valid = args == (101, c['tg_poll_id'])
        elif 'WHERE id=$1 AND owner=$2' in sql:
            valid = args == (8, 101)
        else:
            valid = args == ('d', 101, '42')
        return deepcopy(c) if valid else None

    async def execute(self, sql, *args):
        if 'SET tg_message_id=$3,tg_poll_id=$4' in sql:
            assert args[:2] == (8, 101)
            self.card.update(tg_message_id=args[2], tg_poll_id=args[3], poll_signature=args[4], poll_closed=args[5])
        elif 'SET poll_closed=true' in sql:
            self.card['poll_closed'] = True
        elif 'SET tg_message_id=NULL' in sql:
            self.card.update(tg_message_id=None, tg_poll_id=None, poll_signature=None, poll_closed=False)
        return 'UPDATE 1'

    async def put_card(self, d, mid, kind, data):
        assert d['owner'] == 101 and mid == '42' and kind == 'poll'
        if not self.card:
            self.card = {'id': 8, 'owner': 101, 'dialog_id': 'd', 'max_message_id': '42', 'kind': 'poll',
                         'tg_message_id': None, 'tg_poll_id': None, 'poll_signature': None, 'poll_closed': False}
        self.card['data'] = deepcopy(data)
        return deepcopy(self.card)

    def card_data(self, card):
        return deepcopy(card['data'])


@pytest.fixture
def service():
    db = Storage()
    sent = 0
    async def call(method, params):
        nonlocal sent
        if method == 'sendPoll':
            sent += 1
            return {'message_id': 100 + sent, 'poll': {'id': f'poll-{sent}', 'options': [{'persistent_id': 'p1'}, {'persistent_id': 'p2'}]}}
        return True
    tg = NS(call=AsyncMock(side_effect=call), pace=AsyncMock(), remove=AsyncMock(return_value=True))
    lock = asyncio.Lock()
    interactions = NS(db=db, tg=tg, lock=lambda owner: lock, _publish=AsyncMock(return_value={'id': 777, 'kind': 'poll'}))
    return NativePolls(interactions)


def answer(**changes):
    return {'user': {'id': 101}, 'poll_id': 'poll-1', 'option_ids': [1], 'update_id': 123, **changes}


@pytest.mark.asyncio
async def test_publish_native_poll_in_owner_topic_without_fabricating_votes(service):
    result = await service.publish(service.db.dialog, '42', poll(), 7)
    assert result == {'id': 101, 'kind': 'poll'}
    method, params = service.tg.call.call_args.args
    assert method == 'sendPoll' and params['chat_id'] == 101 and params['message_thread_id'] == 7
    assert params['is_anonymous'] is False and params['protect_content'] is True
    assert params['options'] == [{'text': 'Парк'}, {'text': 'Кино'}]
    assert 'voter_count' not in str(params['options'])
    assert service.db.card['tg_poll_id'] == 'poll-1'
    assert service.db.card['data']['poll_id'] == 500
    service.i._publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_vote_maps_option_index_to_original_max_answer_and_queues(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer())
    d, direction, action, source, revision, parts = service.db.enqueue.call_args.args
    assert (d['owner'], direction, action, source, revision) == (101, 'max', 'vote', '42', 'telegram:poll:123')
    assert parts == [{'max_id': '42', 'poll_id': 500, 'answer_ids': [80], 'native_signature': poll_signature(poll())}]


@pytest.mark.asyncio
async def test_persistent_ids_survive_snapshot_refresh(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer(option_ids=[0], option_persistent_ids=['p2']))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == [80]
    assert [c.args[0] for c in service.tg.call.await_args_list].count('sendPoll') == 1


@pytest.mark.parametrize('changes', [
    {'user': {'id': 202}}, {'user': {'id': 101, 'is_bot': True}},
    {'voter_chat': {'id': 777}}, {'poll_id': 'foreign'}, {'user': {}},
])
@pytest.mark.asyncio
async def test_foreign_anonymous_and_unknown_poll_answers_are_ignored(service, changes):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer(**changes))
    service.db.enqueue.assert_not_awaited()


@pytest.mark.parametrize('options', [[-1], [2], [True], ['1'], [0, 0], [0, 1], None])
@pytest.mark.asyncio
async def test_invalid_or_multi_selection_for_single_choice_rejected(service, options):
    await service.publish(service.db.dialog, '42', poll(), 7)
    with pytest.raises(Rejected):
        await service.answer(answer(option_ids=options))
    service.db.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiselect_and_retraction(service):
    await service.publish(service.db.dialog, '42', poll(7), 7)
    await service.answer(answer(option_ids=[1, 0]))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == [20, 80]
    await service.answer(answer(option_ids=[], update_id=124))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == []


@pytest.mark.asyncio
async def test_closed_original_stops_existing_native_poll_without_new_poll(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.publish(service.db.dialog, '42', poll(13), 7)
    assert service.db.card['poll_closed']
    assert [c.args[0] for c in service.tg.call.await_args_list].count('stopPoll') == 1
    assert [c.args[0] for c in service.tg.call.await_args_list].count('sendPoll') == 1
    with pytest.raises(Rejected):
        await service.answer(answer())


@pytest.mark.asyncio
async def test_changed_poll_invalidates_previous_local_id(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    changed = poll()
    changed['answers'].reverse()
    await service.publish(service.db.dialog, '42', changed, 7)
    assert service.db.card['tg_poll_id'] == 'poll-2'
    await service.answer(answer())
    service.db.enqueue.assert_not_awaited()
    await service.answer(answer(poll_id='poll-2'))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == [20]


@pytest.mark.asyncio
async def test_quiz_without_known_correct_option_uses_explicit_card(service):
    await service.publish(service.db.dialog, '42', poll(16), 7)
    service.i._publish.assert_awaited_once()
    service.tg.call.assert_not_awaited()


@pytest.mark.parametrize('update', [
    {'title': 'x' * 301}, {'answers': []}, {'answers': [{'text': 'x'*101, 'answer_id': 1}]},
    {'answers': [{'text': 'A', 'answer_id': None}]},
    {'answers': [{'text': 'A', 'answer_id': 1}, {'text': 'B', 'answer_id': 1}]},
])
def test_native_limits_fallback_without_truncation(update):
    assert native_parameters({**poll(), **update}) is None


@pytest.mark.asyncio
async def test_results_popup_is_owner_scoped_and_schedules_refresh(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    q = {'id': 'cb', 'from': {'id': 101}, 'message': {'chat': {'id': 101, 'type': 'private'},
         'message_id': 101, 'message_thread_id': 7}, 'data': f'np:8:{poll_signature(poll())}'}
    await service.callback(q)
    assert service.db.enqueue.call_args.args[2] == 'poll_refresh'
    method, params = service.tg.call.call_args.args
    assert method == 'answerCallbackQuery' and params['show_alert'] is True
    assert '9 голосов' in params['text'] and 'Парк — 7' in params['text']
    q['from']['id'] = 202
    with pytest.raises(Rejected):
        await service.callback(q)
    assert service.db.enqueue.await_count == 1


def test_long_results_alert_is_bounded():
    data = poll()
    data['answers'] = [{'answer_id': n, 'text': 'x'*100} for n in range(12)]
    assert len(results_text(data)) <= 200


@pytest.mark.parametrize('counters,expected,result', [
    ([{'reaction': '👍', 'count': 1}], [{'type': 'emoji', 'emoji': '👍'}], True),
    ([{'reaction': '👍', 'count': 2}], [], False),
    ([{'reaction': '👍', 'count': 1}, {'reaction': '❤️', 'count': 1}], [], False),
    ([], [], True),
])
@pytest.mark.asyncio
async def test_native_reactions_never_forge_aggregate_counts(counters, expected, result):
    tg = NS(call=AsyncMock())
    assert await mirror_reactions(tg, 101, 500, counters) is result
    tg.call.assert_awaited_once_with('setMessageReaction', {'chat_id': 101, 'message_id': 500, 'reaction': expected, 'is_big': False})


@pytest.mark.asyncio
async def test_unsupported_reaction_falls_back_but_permissions_are_not_swallowed():
    tg = NS(call=AsyncMock(side_effect=TelegramRejected(400, 'REACTION_INVALID')))
    assert await mirror_reactions(tg, 101, 500, [{'reaction': 'x', 'count': 1}]) is False
    tg.call.side_effect = TelegramRejected(403, 'Forbidden')
    with pytest.raises(TelegramRejected):
        await mirror_reactions(tg, 101, 500, [])
