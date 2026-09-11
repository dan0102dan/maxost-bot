"""Mirror only reactions that one bot reaction can represent without false counts."""
from .telegram import TelegramRejected


async def mirror_reactions(tg, owner, message_id, counters):
    single = len(counters) == 1 and counters[0]['count'] == 1
    reaction = [{'type': 'emoji', 'emoji': counters[0]['reaction']}] if single else []
    try:
        await tg.call('setMessageReaction', {
            'chat_id': owner, 'message_id': message_id, 'reaction': reaction, 'is_big': False})
    except TelegramRejected as exc:
        if exc.code == 400:
            return False  # Unsupported MAX emoji or a Telegram message without reactions.
        raise
    return single or not counters
