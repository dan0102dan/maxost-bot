"""Native poll/reaction behavior without contacting Telegram or MAX."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost.errors import Rejected
from maxost.native_polls import (
    NativePolls,
    fallback_text,
    native_parameters,
    poll_signature,
    results_text,
)
from maxost.native_reactions import mirror_reactions
from maxost.telegram import TelegramRejected


def poll(flags=5):
    return {
        'poll_id': 500,
        'title': 'Куда?',
        'settings': flags,
        'answers': [
            {'answer_id': 20, 'text': 'Парк'},
            {'answer_id': 80, 'text': 'Кино'},
        ],
        'state': {
            'total': 9,
            'result': [
                {'answer_id': 20, 'vote_count': 7},
                {'answer_id': 80, 'vote_count': 2},
            ],
        },
    }


class Storage:
    def __init__(self):
        self.mirror = None
        self.dialog = {
            'id': 'd',
            'owner': 101,
            'tg_thread_id': 7,
            'max_chat_id': 900,
        }
        self.pool = NS(
            fetchrow=AsyncMock(side_effect=self.fetchrow),
            execute=AsyncMock(side_effect=self.execute),
        )
        self.enqueue = AsyncMock()

    async def fetchrow(self, sql, *args):
        if 'FROM dialogs' in sql:
            return dict(self.dialog) if args == ('d', 101) else None
        mirror = self.mirror
        if not mirror:
            return None
        if 'owner=$1 AND tg_poll_id=$2' in sql:
            valid = args == (101, mirror['tg_poll_id'])
        elif 'WHERE id=$1 AND owner=$2' in sql:
            valid = args == (8, 101)
        else:
            valid = args == ('d', 101, '42')
        return deepcopy(mirror) if valid else None

    async def execute(self, sql, *args):
        if 'SET tg_message_id=$3,tg_poll_id=$4' in sql:
            assert args[:2] == (8, 101)
            self.mirror.update(
                tg_message_id=args[2],
                tg_poll_id=args[3],
                signature=args[4],
                closed=args[5],
            )
        elif 'SET signature=$3,closed=$4' in sql:
            self.mirror.update(signature=args[2], closed=args[3])
        elif 'SET closed=true' in sql:
            self.mirror['closed'] = True
        elif 'SET tg_message_id=$3,tg_poll_id=NULL' in sql:
            self.mirror.update(
                tg_message_id=args[2],
                tg_poll_id=None,
                signature=args[3],
                closed=args[4],
            )
        return 'UPDATE 1'

    async def put_poll(self, dialog, max_message_id, data):
        assert dialog['owner'] == 101 and max_message_id == '42'
        if not self.mirror:
            self.mirror = {
                'id': 8,
                'owner': 101,
                'dialog_id': 'd',
                'max_message_id': '42',
                'tg_message_id': None,
                'tg_poll_id': None,
                'signature': None,
                'closed': False,
            }
        self.mirror['data'] = deepcopy(data)
        return deepcopy(self.mirror)

    def poll_data(self, mirror):
        return deepcopy(mirror['data'])


@pytest.fixture
def service():
    db = Storage()
    sent = 0

    async def call(method, params):
        nonlocal sent
        if method == 'sendPoll':
            sent += 1
            return {
                'message_id': 100 + sent,
                'poll': {
                    'id': f'poll-{sent}',
                    'options': [
                        {'persistent_id': 'p1'},
                        {'persistent_id': 'p2'},
                    ],
                },
            }
        if method == 'sendMessage':
            sent += 1
            return {'message_id': 100 + sent}
        return True

    tg = NS(
        call=AsyncMock(side_effect=call),
        pace=AsyncMock(),
        remove=AsyncMock(return_value=True),
    )
    lock = asyncio.Lock()
    interactions = NS(db=db, tg=tg, lock=lambda owner: lock)
    return NativePolls(interactions)


def answer(**changes):
    return {
        'user': {'id': 101},
        'poll_id': 'poll-1',
        'option_ids': [1],
        'update_id': 123,
        **changes,
    }


@pytest.mark.asyncio
async def test_publish_native_poll_in_owner_topic(service):
    result = await service.publish(service.db.dialog, '42', poll(), 7)
    assert result == {'id': 101, 'kind': 'poll'}
    method, params = service.tg.call.call_args.args
    assert method == 'sendPoll'
    assert params['chat_id'] == 101 and params['message_thread_id'] == 7
    assert params['is_anonymous'] is False and params['protect_content'] is True
    assert params['options'] == [{'text': 'Парк'}, {'text': 'Кино'}]
    assert service.db.mirror['tg_poll_id'] == 'poll-1'
    assert service.db.mirror['data']['poll_id'] == 500


@pytest.mark.asyncio
async def test_vote_maps_option_to_original_max_answer(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer())
    dialog, direction, action, source, revision, parts = service.db.enqueue.call_args.args
    assert (dialog['owner'], direction, action, source, revision) == (
        101,
        'max',
        'vote',
        '42',
        'telegram:poll:123',
    )
    assert parts == [{
        'max_id': '42',
        'poll_id': 500,
        'answer_ids': [80],
        'native_signature': poll_signature(poll()),
    }]


@pytest.mark.asyncio
async def test_persistent_option_ids_survive_refresh(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer(option_ids=[0], option_persistent_ids=['p2']))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == [80]
    assert [call.args[0] for call in service.tg.call.await_args_list].count('sendPoll') == 1


@pytest.mark.parametrize(
    'changes',
    [
        {'user': {'id': 202}},
        {'user': {'id': 101, 'is_bot': True}},
        {'voter_chat': {'id': 777}},
        {'poll_id': 'foreign'},
        {'user': {}},
    ],
)
@pytest.mark.asyncio
async def test_foreign_anonymous_and_unknown_answers_are_ignored(service, changes):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.answer(answer(**changes))
    service.db.enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    'options',
    [[-1], [2], [True], ['1'], [0, 0], [0, 1], None],
)
@pytest.mark.asyncio
async def test_invalid_or_multi_selection_rejected(service, options):
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
async def test_closed_original_stops_existing_native_poll(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    await service.publish(service.db.dialog, '42', poll(13), 7)
    assert service.db.mirror['closed']
    assert [call.args[0] for call in service.tg.call.await_args_list].count('stopPoll') == 1
    with pytest.raises(Rejected):
        await service.answer(answer())


@pytest.mark.asyncio
async def test_changed_poll_replaces_native_poll(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    changed = poll()
    changed['answers'].reverse()
    await service.publish(service.db.dialog, '42', changed, 7)
    assert service.db.mirror['tg_poll_id'] == 'poll-2'
    await service.answer(answer())
    service.db.enqueue.assert_not_awaited()
    await service.answer(answer(poll_id='poll-2'))
    assert service.db.enqueue.call_args.args[-1][0]['answer_ids'] == [20]


@pytest.mark.asyncio
async def test_unrepresentable_quiz_uses_plain_text_not_legacy_buttons(service):
    await service.publish(service.db.dialog, '42', poll(16), 7)
    method, params = service.tg.call.call_args.args
    assert method == 'sendMessage'
    assert 'Викторина MAX' in params['text']
    assert 'reply_markup' not in params
    assert service.db.mirror['tg_poll_id'] is None


@pytest.mark.parametrize(
    'update',
    [
        {'title': 'x' * 301},
        {'answers': []},
        {'answers': [{'text': 'x' * 101, 'answer_id': 1}]},
        {'answers': [{'text': 'A', 'answer_id': None}]},
        {
            'answers': [
                {'text': 'A', 'answer_id': 1},
                {'text': 'B', 'answer_id': 1},
            ]
        },
    ],
)
def test_native_limits_are_explicit(update):
    data = {**poll(), **update}
    assert native_parameters(data) is None
    assert 'нельзя воспроизвести нативно' in fallback_text(data)


@pytest.mark.asyncio
async def test_results_popup_is_owner_scoped_and_schedules_refresh(service):
    await service.publish(service.db.dialog, '42', poll(), 7)
    query = {
        'id': 'cb',
        'from': {'id': 101},
        'message': {
            'chat': {'id': 101, 'type': 'private'},
            'message_id': 101,
            'message_thread_id': 7,
        },
        'data': f'np:8:{poll_signature(poll())}',
    }
    await service.callback(query)
    assert service.db.enqueue.call_args.args[2] == 'poll_refresh'
    method, params = service.tg.call.call_args.args
    assert method == 'answerCallbackQuery' and params['show_alert'] is True
    assert '9 голосов' in params['text'] and 'Парк — 7' in params['text']
    query['from']['id'] = 202
    with pytest.raises(Rejected):
        await service.callback(query)


def test_long_results_alert_is_bounded():
    data = poll()
    data['answers'] = [
        {'answer_id': index, 'text': 'x' * 100} for index in range(12)
    ]
    assert len(results_text(data)) <= 200


@pytest.mark.parametrize(
    'counters,expected,result',
    [
        ([{'reaction': '👍', 'count': 1}], [{'type': 'emoji', 'emoji': '👍'}], True),
        ([{'reaction': '👍', 'count': 2}], [], False),
        (
            [
                {'reaction': '👍', 'count': 1},
                {'reaction': '❤️', 'count': 1},
            ],
            [],
            False,
        ),
        ([], [], True),
    ],
)
@pytest.mark.asyncio
async def test_native_reactions_never_forge_aggregate_counts(
    counters, expected, result
):
    tg = NS(call=AsyncMock())
    assert await mirror_reactions(tg, 101, 500, counters) is result
    tg.call.assert_awaited_once_with(
        'setMessageReaction',
        {
            'chat_id': 101,
            'message_id': 500,
            'reaction': expected,
            'is_big': False,
        },
    )


@pytest.mark.asyncio
async def test_unsupported_reaction_does_not_hide_permission_errors():
    tg = NS(call=AsyncMock(side_effect=TelegramRejected(400, 'REACTION_INVALID')))
    assert await mirror_reactions(
        tg, 101, 500, [{'reaction': 'x', 'count': 1}]
    ) is False
    tg.call.side_effect = TelegramRejected(403, 'Forbidden')
    with pytest.raises(TelegramRejected):
        await mirror_reactions(tg, 101, 500, [])
