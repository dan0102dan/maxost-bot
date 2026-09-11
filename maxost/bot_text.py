"""User-facing text, separate from operator diagnostics."""

HELP = (
    'MAXOST · MAX в Telegram\n\n'
    'Отвечайте в топике нужного чата. Файлы и опросы отправляйте как обычно.\n\n'
    '/connect — подключить MAX\n'
    '/status — проверить подключение\n'
    '/disconnect — отключить MAX\n'
    '/cancel — отменить вход\n'
    '/delete — удалить из MAX (ответом на своё сообщение)\n\n'
    'Повтор и пропуск — кнопками под ошибкой.'
)


def status_text(state, counts):
    states = {
        'connected': '🟢 MAX подключён',
        'offline': '🟡 Переподключаемся к MAX',
        'reauth': '🔑 Войдите в MAX заново: /disconnect → /connect',
        'authorizing': '⏳ Ожидаем подтверждение входа',
        'paused': '⏸ MAX отключён',
    }
    lines = [states.get(state, 'MAX не подключён. /connect')]
    queued = sum(counts.get(key, 0) for key in ('pending', 'claimed', 'sending'))
    if queued:
        lines.append(f'В очереди: {queued}')
    if counts.get('failed'):
        lines.append(f'Не доставлено: {counts["failed"]}')
    if counts.get('unknown'):
        lines.append(f'Нужно проверить доставку: {counts["unknown"]}')
    if counts.get('failed') or counts.get('unknown'):
        lines.append('Повторить или пропустить — кнопками под ошибкой.')
    return '\n'.join(lines)


def auth_notice(screen):
    """Show an actionable first line; keep detailed categories in server logs."""
    if screen.phase == 'cancelled':
        return 'Вход отменён.'
    if screen.phase == 'done':
        return ('MAX отключён. Завершите сессию также в MAX → Устройства.'
                if 'Устройства' in screen.notice else 'MAX отключён.')
    text = screen.notice.split('\nДиагностика:', 1)[0]
    if screen.phase == 'error':
        return (text or 'Не удалось подключиться. Попробуйте ещё раз.') + f'\nКод: {screen.trace_id}'
    return text
