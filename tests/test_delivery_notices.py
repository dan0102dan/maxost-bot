"""Keep actionable, user-safe failure reasons beside the inline controls."""
import base64
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from maxost.crypto import Vault
from maxost.queue_actions import QueueActions, error_text
from maxost.telegram import TelegramRejected


REASONS = [
    'Канал доступен только для чтения: у вашего MAX-аккаунта нет прав публикации.',
    'Вложение превышает настроенный лимит размера.',
    'Топик закрыт. Откройте его и повторите отправку.',
    'Результат создания топика неизвестен. Отправьте /bind 12345678-1234-1234-1234-123456789012.',
]


def failed_job(reason, direction='tg'):
    return {
        'id': 42, 'owner': 101, 'direction': direction, 'status': 'failed',
        'updated_at': datetime.now(timezone.utc), 'attempts': 2,
        'payload': b'encrypted-original-content', 'source_id': '99',
        'tg_thread_id': 7, 'error': reason,
    }


def actions():
    db = NS(
        vault=Vault(base64.urlsafe_b64encode(b'x' * 32).decode()),
        pool=NS(execute=AsyncMock()),
    )
    tg = NS(call=AsyncMock(return_value={'message_id': 500}), pace=AsyncMock())
    return QueueActions(db, tg)


@pytest.mark.parametrize('direction', ['tg', 'max'])
@pytest.mark.parametrize('reason', REASONS)
@pytest.mark.asyncio
async def test_stored_failure_reason_is_in_separate_notice_with_both_buttons(direction, reason):
    service = actions()
    job = failed_job(reason, direction)
    before = dict(job)
    await service.notify(job)
    method, params = service.tg.call.await_args.args
    destination = 'Telegram' if direction == 'tg' else 'MAX'
    assert method == 'sendMessage'
    assert params['text'] == f'Не удалось отправить сообщение №42 в {destination}.\n{reason}'
    assert params['chat_id'] == 101 and params['message_thread_id'] == 7
    buttons = params['reply_markup']['inline_keyboard'][0]
    assert [(button['text'], button['style']) for button in buttons] == [
        ('Повторить', 'primary'), ('Пропустить', 'danger'),
    ]
    assert 'parse_mode' not in params
    assert '/retry' not in params['text'] and '/skip' not in params['text']
    assert job == before


@pytest.mark.parametrize('reason', [None, '', '   \n\t '])
def test_missing_reason_has_clean_generic_notice(reason):
    assert error_text(failed_job(reason)) == 'Не удалось отправить сообщение №42 в Telegram.'


def test_multiline_reason_is_compact_and_long_reason_is_bounded():
    assert error_text(failed_job(' Файл недоступен.\n  Проверьте MAX.\t')).endswith(
        '\nФайл недоступен. Проверьте MAX.'
    )
    text = error_text(failed_job('Причина ' * 2000))
    assert text.endswith('…') and len(text) < 400


@pytest.mark.asyncio
async def test_missing_topic_fallback_keeps_reason_and_controls_in_root_chat():
    service = actions()
    service.tg.call.side_effect = [
        TelegramRejected(400, 'Bad Request: message thread not found'),
        {'message_id': 500},
    ]
    job = failed_job(REASONS[2], direction='max')
    await service.notify(job)
    first, second = [call.args[1] for call in service.tg.call.await_args_list]
    # Both calls carry the stored cause, even when the topic itself is unavailable.
    assert REASONS[2] in first['text'] and second['text'] == first['text']
    assert second['reply_markup'] == service.markup(job)
    assert 'message_thread_id' not in second and 'reply_parameters' not in second
    assert second['chat_id'] == job['owner']
    service.db.pool.execute.assert_awaited_once()
