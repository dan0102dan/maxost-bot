from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid

from .content import from_telegram, is_dialog, supported_chat, chat_kind, can_publish
from .errors import Rejected, RetryLater, Uncertain
from .max_client import SessionRevoked, is_revoked
from .delivery import Delivery
from .interactions import Interactions
from .formatting import from_max as max_entities, prepend, value
from .queue_actions import QueueActions
from .telegram import TelegramRejected

log=logging.getLogger(__name__)


def millis(value):
    value=int(value or 0)
    return value*1000 if 0<value<100_000_000_000 else value


def revision(payload):
    # Revision IDs need no plaintext in DB and are scoped by account/dialog/source.
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def normalize_max(message):
    if getattr(message, 'ttl', False):
        return [{'text': '[Временное сообщение MAX: содержимое не копируется]', 'entities': [], 'attachments': []}]
    text = getattr(message, 'text', None) or ''
    entities, unsupported = max_entities(text, getattr(message, 'elements', []))
    attachments = []
    poll_seen = False
    for index, a in enumerate(getattr(message, 'attaches', []) or []):
        kind = str(getattr(value(a, 'type'), 'value', value(a, 'type', ''))).upper()
        if kind in ('PHOTO', 'FILE', 'VIDEO', 'AUDIO', 'STICKER', 'POLL'):
            item = {'index': index, 'kind': kind.lower(),
                    'identity': next((str(value(a, key)) for key in ('photo_id', 'video_id', 'file_id', 'sticker_id', 'poll_id') if value(a, key) is not None), None)}
            if kind == 'POLL' and poll_seen:
                text += ('\n[Дополнительный опрос MAX: ' + str(value(a, 'title', '')) +
                    '. Голосование доступно в MAX.]')
                continue
            if kind == 'POLL':
                poll_seen = True
                item['poll'] = a.model_dump(mode='json') if hasattr(a, 'model_dump') else vars(a)
            attachments.append(item)
        else:
            text += f'\n[Вложение MAX: {kind or "неизвестный тип"}. Откройте его в MAX.]'
    if unsupported:
        text += '\n[Формат MAX без аналога Telegram: ' + ', '.join(unsupported) + ']'
    if not text and not attachments:
        return []
    link = getattr(message, 'link', None)
    reply = None
    if link and str(getattr(value(link, 'type'), 'value', value(link, 'type', ''))).upper() == 'REPLY':
        if value(link, 'chat_id', value(message, 'chat_id')) in (None, value(message, 'chat_id')):
            reply = value(value(link, 'message'), 'id')
    return [{'text': text, 'entities': entities, 'attachments': attachments, 'reply_to': reply}]


class Bridge:
    def __init__(self,db,tg,hub,media,settings):
        self.db,self.tg,self.hub,self.media,self.settings=db,tg,hub,media,settings
        self.ingest_locks={}
        self.stopped=False
        self.delivery = Delivery(self)
        self.interactions = Interactions(self)
        self.queue_actions = QueueActions(db, tg)
        self.delivery.interactions = self.interactions
        hub.on_event,hub.on_history=self.from_max,self.recover_history

    async def from_max(self,entry,action,message):
        a=entry.account
        chat_id=getattr(message,'chat_id',None)
        if chat_id is None or entry.closing:
            return
        async with self.ingest_locks.setdefault(a['id'],asyncio.Lock()):
            if action=='reaction':
                return await self.interactions.schedule(entry, message.chat_id, message.message_id, 'reaction')
            if action=='delete':
                d=await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE account_id=$1 AND owner=$2 AND max_chat_id=$3',a['id'],a['owner'],chat_id)
                if d:
                    for mid in message.message_ids:
                        await self.db.enqueue(d,'tg','delete',str(mid),'',[{}])
                return
            # Intentional product behavior: never mirror messages written by the owner in MAX.
            # A channel can omit sender; locally registered CIDs suppress our anonymous echo.
            if getattr(message, 'sender', None) == a['max_user_id'] or getattr(message, 'cid', None) in getattr(entry.client, '_maxost_sent_cids', set()):
                return
            if millis(getattr(message,'time',0)) < a['since_ms']:
                return
            chat=await entry.client.get_chat(chat_id)
            if not supported_chat(chat):
                return
            title=chat.title
            if not title and message.sender is not None:
                user=await entry.client.get_user(message.sender)
                # User.names is a list of structured Name objects in PyMax.
                names=getattr(user,'names',[]) or []
                name=names[0] if names else None
                title=' '.join(filter(None,[getattr(name,'first_name',None),getattr(name,'last_name',None)]))
            title=title or f'MAX · {message.sender}'
            d=await self.db.dialog(a,chat_id,message.sender if is_dialog(chat) else None,title)
            await self.db.pool.execute('UPDATE dialogs SET chat_kind=$3 WHERE id=$1 AND owner=$2',d['id'],a['owner'],chat_kind(chat))
            parts=normalize_max(message)
            if chat_kind(chat) == 'CHAT' and parts:
                author = f'Участник {message.sender}' if message.sender is not None else 'Группа MAX'
                if message.sender is not None:
                    user = await entry.client.get_user(message.sender)
                    names = getattr(user, 'names', []) or []
                    if names:
                        author = ' '.join(filter(None, [value(names[0], 'first_name'), value(names[0], 'last_name')])) or author
                parts[0] = prepend(parts[0], author + '\n')
            if parts:
                if action=='edit':
                    await self.db.enqueue(d,'tg','edit',str(message.id),uuid.uuid4().hex,[{'parts':parts}])
                else:
                    await self.db.enqueue(d,'tg','send',str(message.id),'',parts)

    async def from_telegram(self,message,edited=False,event_id=0):
        owner=message['from']['id']
        thread=message.get('message_thread_id')
        d=await self.db.by_thread(owner,thread) if thread else None
        if not d:
            raise Rejected('Этот топик не связан с MAX. Дождитесь входящего сообщения или используйте /help.')
        account=await self.db.account(owner)
        if not account or account['id']!=d['account_id']:
            raise Rejected('Аккаунт отключён.')
        if account['status'] in ('reauth','paused','authorizing'):
            raise Rejected('MAX требует повторного входа. Проверьте /status.')
        parts=from_telegram(message,self.settings.max_file_bytes)
        group_id = message.get('media_group_id')
        if edited and not group_id:
            links = await self.db.links(d, 'tg', message['message_id'])
            source_key = links[0].get('source_key', '') if links else ''
            if source_key.startswith('album:'):
                group_id = source_key.removeprefix('album:')
        if group_id:
            version = int(message.get('edit_date', message.get('date', 0))) * 10_000_000 + event_id % 10_000_000
            await self.db.collect_album(d, str(group_id), message['message_id'], parts[0], version)
            return
        if edited:
            await self.db.enqueue(d,'max','edit',str(message['message_id']),str(event_id)+':'+str(message.get('edit_date',0))+':'+revision(parts),[{'parts':parts}])
        else:
            await self.db.enqueue(d,'max','send',str(message['message_id']),'',parts)

    async def delete_from_telegram(self,message):
        owner=message['from']['id']
        d=await self.db.by_thread(owner,message.get('message_thread_id'))
        mid=message.get('reply_to_message',{}).get('message_id')
        if not d or not mid:
            raise Rejected('Ответьте командой /delete на своё сообщение в топике MAX.')
        links=await self.db.links(d,'tg',mid)
        if not links or any(link['origin']!='tg' for link in links):
            raise Rejected('Удалять можно только собственные сообщения, отправленные этим мостом.')
        await self.db.enqueue(d,'max','delete',links[0].get('source_key') or str(mid),'',[{}])

    async def topic(self,d):
        d=await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2',d['id'],d['owner'])
        if d['topic_state']=='ready' and d['tg_thread_id'] is not None:
            return d['tg_thread_id']
        if d['topic_state'] in ('creating','unknown'):
            raise Rejected(f"Результат создания топика неизвестен. Найдите его и отправьте в нём /bind {d['id']}.")
        await self.db.pool.execute("UPDATE dialogs SET topic_state='creating' WHERE id=$1 AND owner=$2",d['id'],d['owner'])
        title=self.db.vault.open(d['owner'],f"title:{d['account_id']}:{d['max_chat_id']}",bytes(d['title_cipher']))
        name=str(title).replace('\n',' ')[:95]+f" · {str(d['id'])[:8]}"
        try:
            await self.tg.pace(d['owner'])
            result=await self.tg.call('createForumTopic',{'chat_id':d['owner'],'name':name})
        except (TelegramRejected,RetryLater):
            await self.db.pool.execute("UPDATE dialogs SET topic_state='pending' WHERE id=$1 AND owner=$2",d['id'],d['owner'])
            raise
        except BaseException:
            await self.db.pool.execute("UPDATE dialogs SET topic_state='unknown' WHERE id=$1 AND owner=$2",d['id'],d['owner'])
            raise
        thread=result['message_thread_id']
        await self.db.pool.execute("UPDATE dialogs SET topic_state='ready',tg_thread_id=$3 WHERE id=$1 AND owner=$2",d['id'],d['owner'],thread)
        return thread

    async def bind(self,owner,did,thread):
        if not thread:
            raise Rejected('Команду /bind нужно отправить внутри существующего топика.')
        try:
            did=uuid.UUID(did)
        except ValueError:
            raise Rejected('Неверный идентификатор диалога.') from None
        async with self.db.pool.acquire() as c,c.transaction():
            d=await c.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2 FOR UPDATE',did,owner)
            if not d or d['topic_state'] not in ('unknown','pending'):
                raise Rejected('Диалог не найден или уже привязан.')
            if await c.fetchval('SELECT 1 FROM dialogs WHERE owner=$1 AND tg_thread_id=$2',owner,thread):
                raise Rejected('Этот топик уже используется другим диалогом.')
            await c.execute("UPDATE dialogs SET tg_thread_id=$3,topic_state='ready' WHERE id=$1 AND owner=$2",did,owner,thread)
        # Binding is not sending; failed jobs still require the Retry button.

    async def recover_history(self,entry):
        a=await self.db.account(entry.account['owner'])
        if not a or entry.closing:
            return
        start=max(a['since_ms'],a['history_ms']-1000)
        end=int(time.time()*1000)
        chats={c.id:c for c in (entry.client.chats or []) if supported_chat(c)}
        marker=None
        # Bounded work per pass, but never advance the checkpoint on an incomplete pass.
        for _ in range(self.settings.history_pages):
            page=await entry.client.fetch_chats(marker=marker)
            if not page:
                break
            chats.update({c.id:c for c in page if supported_chat(c)})
            oldest=min(millis(c.last_event_time) for c in page)
            if oldest<=start:
                break
            if marker is not None and oldest>=marker:
                raise RetryLater('MAX не продвинул маркер списка чатов.',30)
            marker=oldest
        else:
            raise RetryLater('Достигнут лимит страниц восстановления; увеличьте HISTORY_MAX_PAGES.',30)
        for chat in sorted(chats.values(),key=lambda c:c.id):
            if millis(chat.last_event_time)<start:
                continue
            cursor=end
            buffered={}
            for _ in range(self.settings.history_pages):
                page=await entry.client.fetch_history(chat_id=chat.id,from_time=cursor,backward=40,forward=0)
                if not page:
                    break
                for message in page:
                    t=millis(message.time)
                    if start<=t<=end:
                        if message.chat_id is None:
                            message=message.model_copy(update={'chat_id':chat.id})
                        buffered[message.id]=message
                oldest=min(millis(m.time) for m in page)
                if oldest<=start or len(page)<40:
                    break
                if oldest>=cursor:
                    # Do not skip messages sharing an ambiguous pagination timestamp.
                    raise RetryLater('MAX не продвинул маркер истории.',30)
                cursor=oldest
            else:
                raise RetryLater('Достигнут лимит истории; контрольная точка не сдвинута.',30)
            for message in sorted(buffered.values(),key=lambda m:(millis(m.time),m.id)):
                await self.from_max(entry,'send',message)
        await self.db.pool.execute('UPDATE accounts SET history_ms=$3 WHERE id=$1 AND owner=$2',a['id'],a['owner'],end)

    async def worker(self):
        while not self.stopped:
            job=await self.db.claim()
            if not job:
                await asyncio.sleep(0.4)
                continue
            external=False
            try:
                d=await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2',job['dialog_id'],job['owner'])
                if not d:
                    continue
                entry=self.hub.get(d['account_id'],d['owner'])
                async with entry.gate:
                    self.hub.get(d['account_id'],d['owner'])
                    p=self.db.payload(job)
                    if job['direction'] == 'max' and job['action'] in ('send','edit'):
                        chat = await entry.client.get_chat(d['max_chat_id'])
                        if not can_publish(chat, entry.account['max_user_id']):
                            raise Rejected('Канал доступен только для чтения: у вашего MAX-аккаунта нет прав публикации.')
                    await self.db.mark_sending(job)
                    external = True
                    if job['action'] == 'send':
                        await self.delivery.send(entry, d, job, p)
                    else:
                        await self.delivery.change(entry, d, job, p)
                    await self.db.complete(job)
            except asyncio.CancelledError:
                # recover() classifies sending as unknown on the next start.
                raise
            except RetryLater as exc:
                await self.db.state(job,'pending',str(exc),min(max(exc.delay,1),300))
            except Uncertain as exc:
                await self.db.state(job,'unknown',str(exc))
            except TelegramRejected as exc:
                description=exc.description.lower()
                if job['direction']=='tg' and 'message thread not found' in description:
                    await self.db.pool.execute("UPDATE dialogs SET topic_state='pending',tg_thread_id=NULL WHERE id=$1 AND owner=$2",job['dialog_id'],job['owner'])
                    await self.db.state(job,'pending','Топик удалён; будет создан заново.',2)
                elif job['direction']=='tg' and ('topic_closed' in description or 'topic is closed' in description):
                    with contextlib.suppress(Exception):
                        await self.tg.call('reopenForumTopic',{'chat_id':job['owner'],'message_thread_id':d['tg_thread_id']})
                    await self.db.state(job,'failed','Топик закрыт. Откройте его и нажмите «Повторить».')
                else:
                    await self.db.state(job,'failed',str(exc))
            except SessionRevoked as exc:
                await self.hub.reauth(entry)
                await self.db.state(job,'failed',str(exc))
            except Rejected as exc:
                await self.db.state(job,'failed',str(exc))
            except Exception as exc:
                if is_revoked(exc):
                    await self.hub.reauth(entry)
                    await self.db.state(job, 'failed', str(SessionRevoked()))
                    continue
                log.warning('Delivery failed (%s), job=%s',type(exc).__name__,job['id'])
                await self.db.state(job,'unknown' if external else 'failed','Не удалось подтвердить результат.' if external else 'Ошибка подготовки сообщения.')

    async def change(self,entry,d,job,p):
        return await self.delivery.change(entry,d,job,p)

    async def notify_errors(self):
        for a in await self.db.pool.fetch("SELECT id,owner FROM accounts WHERE status='reauth' AND NOT reauth_notified"):
            try:
                await self.tg.text(a['owner'], 'Сессия MAX отозвана. Войдите заново: /disconnect → /connect.')
            except Exception:
                continue
            await self.db.pool.execute("UPDATE accounts SET reauth_notified=true WHERE id=$1 AND owner=$2 AND status='reauth'", a['id'], a['owner'])
        rows=await self.db.pool.fetch("""SELECT j.*,d.tg_thread_id FROM jobs j
            JOIN dialogs d ON d.id=j.dialog_id AND d.owner=j.owner
            WHERE j.status IN ('failed','unknown','skipped') AND j.error IS NOT NULL AND NOT j.notified
            ORDER BY j.id LIMIT 20""")
        for job in rows:
            try:
                await self.queue_actions.notify(job)
            except Exception as exc:
                # Notifications cannot hold up delivery; retry only the notice later.
                log.warning('Delivery notice failed (%s), job=%s', type(exc).__name__, job['id'])
