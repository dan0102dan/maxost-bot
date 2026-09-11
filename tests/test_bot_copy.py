import json
from pathlib import Path

import pytest

from maxost.bot_text import HELP, status_text
from maxost.ui import Screen, card


def visible_text(rich):
    parts = []

    def walk(blocks):
        for block in blocks:
            if block['type'] == 'details':
                parts.append(block['summary'])
                walk(block['blocks'])
                continue
            text = block.get('text')
            if isinstance(text, dict):
                text = text.get('text')
            if text:
                parts.append(text)

    walk(rich['blocks'])
    return '\n'.join(parts)


def test_healthy_status_is_exactly_one_line():
    assert status_text(
        'connected', {'pending': 0, 'failed': 0, 'unknown': 0}
    ) == '🟢 MAX подключён'


def test_status_shows_only_relevant_counters():
    text = status_text('offline', {'pending': 2, 'claimed': 1, 'sending': 1})
    assert text.splitlines() == ['🟡 Переподключаемся к MAX', 'В очереди: 4']
    assert 'истори' not in text.lower() and 'ошиб' not in text.lower()


def test_unknown_delivery_keeps_explicit_duplicate_warning():
    text = status_text(
        'connected',
        {'failed': 1, 'unknown': 1},
        [
            {'id': 42, 'status': 'failed'},
            {'id': 50, 'status': 'unknown'},
        ],
    )
    assert '/retry 42' in text
    assert '/skip 50' in text
    assert '/retry 50 confirm' in text
    assert 'дубль' in text


@pytest.mark.parametrize(
    'state', ['connected', 'offline', 'reauth', 'authorizing', 'paused']
)
def test_no_zero_counters_and_blank_lines(state):
    text = status_text(state, {})
    assert '\n\n' not in text and ': 0' not in text


def test_readme_has_no_pre_release_compatibility_content():
    readme = (Path(__file__).parents[1] / 'README.md').read_text()
    for unwanted in (
        '![CI]',
        'badge.svg',
        '**Так задумано',
        'Typing/',
        'Обновление',
        '`/react`',
        '`/poll`',
        'Совместимость',
    ):
        assert unwanted not in readme
    assert 'топиках' in readme
    assert 'по темам' not in readme


def test_help_is_brief_and_has_no_legacy_commands():
    assert '/react' not in HELP and '/poll' not in HELP and '/forget' not in HELP
    assert len(HELP) < 400


def test_ready_screen_contains_no_history_or_diagnostics():
    text = visible_text(card(Screen(101, phase='ready')))
    assert 'MAX подключён' in text and 'топике' in text
    assert 'истори' not in text.lower()


def test_error_screen_hides_operator_categories_but_keeps_reference():
    screen = Screen(
        101,
        phase='error',
        trace_id='1234abcd',
        notice=(
            'Нет соединения.\nДиагностика: DNS / UNCLASSIFIED. '
            'Попытка: 1234abcd.'
        ),
    )
    text = visible_text(card(screen))
    assert 'Нет соединения.' in text and '1234abcd' in text
    assert 'UNCLASSIFIED' not in text


def test_consent_keeps_security_disclosure_and_keypads_are_unchanged():
    text = visible_text(card(Screen(101)))
    assert 'оператор' in text and 'доступ' in text and 'свой аккаунт' in text
    for phase in ('phone', 'code'):
        screen = Screen(101, phase=phase, buffer='123456', code_length=6)
        rich = card(screen)
        keys = [
            block for block in rich['blocks'] if block['type'] == 'buttons'
        ]
        assert [len(block['buttons']) for block in keys[:4]] == [3] * 4
        assert any(
            button['callback_data'].endswith(':qr')
            for row in keys
            for button in row['buttons']
        )
        if phase == 'code':
            assert '123456' not in json.dumps(rich)
