from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time

from .content import from_telegram, is_dialog, split_text
from .errors import Rejected, RetryLater, Uncertain
from .max_client import mutate
from .telegram import TelegramRejected

log=logging.getLogger(__name__)


def millis(value):
    value=int(value or 0)
    return value*1000 if 0<value<100_000_000_000 else value


def revision(payload):
    # Revision IDs need no plaintext in DB and are scoped by account/dialog/source.
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def normalize_max(message):
    if getattr(message,'ttl',False):
        return [{'text':'[Временное сообщение MAX: содержимое не копируется]'}]
    text=getattr(message,'text',None) or ''
    parts=[{'text':s} for s in split_text(text)] if text else []
    for index,a in enumerate(getattr(message,'attaches',[]) or []):
        kind=str(getattr(getattr(a,'type',None),'value',getattr(a,'type',''))).upper()
        if kind in ('PHOTO','FILE','VIDEO','AUDIO'):
            parts.append({'text':'','attachment':{'index':index,'kind':kind}})
        else:
            parts.append({'text':f'[Вложение MAX: {kind or "неизвестный тип"}. Откройте его в MAX.]'})
    if not parts:
        return []
    link=getattr(message,'link',None)
    reply=None
    if link and str(getattr(getattr(link,'type',None),'value',getattr(link,'type',''))).upper()=='REPLY':
        original=getattr(link,'message',None)
        # Never map a forward/reference from a different MAX conversation.
        if getattr(link,'chat_id',message.chat_id) in (None,message.chat_id):
            reply=getattr(original,'id',None)
    for p in parts:
        p['reply_to']=reply
    return parts


class Bridge:
    def __init__(self,db,tg,hub,media,settings):
        self.db,self.tg,self.hub,self.media,self.settings=db,tg,hub,media,settings
        self.ingest_locks={}
        self.stopped=False
        hub.on_event,hub.on_history=self.from_max,self.recover_history

    async def from_max(self,entry,action,message):
        a=entry.account
        chat_id=getattr(message,'chat_id',None)
        if chat_id is None or entry.closing:
            return
        async with self.ingest_locks.setdefault(a['id'],asyncio.Lock()):
            if action=='delete':
                d=await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE account_id=$1 AND owner=$2 AND max_chat_id=$3',a['id'],a['owner'],chat_id)
                if d:
                    for mid in message.message_ids:
                        await self.db.enqueue(d,'tg','delete',str(mid),'',[{}])
                return
            # Suppress ALL self messages in v0.1, including the bridge's echo.
            if getattr(message,'sender',None) in (None,a['max_user_id']):
                return
            if millis(getattr(message,'time',0)) < a['since_ms']:
                return
            chat=await entry.client.get_chat(chat_id)
            if not is_dialog(chat):
                return
            title=chat.title
            if not title:
                user=await entry.client.get_user(message.sender)
                # User.names is a list of structured Name objects in PyMax.
                names=getattr(user,'names',[]) or []
                name=names[0] if names else None
                title=' '.join(filter(None,[getattr(name,'first_name',None),getattr(name,'last_name',None)]))
            title=title or f'MAX · {message.sender}'
            d=await self.db.dialog(a,chat_id,message.sender,title)
            parts=normalize_max(message)
            if parts:
                if action=='edit':
                    await self.db.enqueue(d,'tg','edit',str(message.id),self.db.vault.digest(revision(parts)),[{'parts':parts}])
                else:
                    await self.db.enqueue(d,'tg','send',str(message.id),'',parts)

    async def from_telegram(self,message,edited=False):
        owner=message['from']['id']
        thread=message.get('message_thread_id')
        d=await self.db.by_thread(owner,thread) if thread else None
        if not d:
            raise Rejected('Эта тема не связана с MAX. Дождитесь входящего сообщения или используйте /help.')
        account=await self.db.account(owner)
        if not account or account['id']!=d['account_id']:
            raise Rejected('Аккаунт отключён.')
        if account['status'] in ('reauth','paused','authorizing'):
            raise Rejected('MAX требует повторного входа. Проверьте /status.')
        parts=from_telegram(message,self.settings.max_file_bytes)
        if edited:
            await self.db.enqueue(d,'max','edit',str(message['message_id']),str(message.get('edit_date',0))+':'+self.db.vault.digest(revision(parts)),[{'parts':parts}])
        else:
            await self.db.enqueue(d,'max','send',str(message['message_id']),'',parts)

    async def delete_from_telegram(self,message):
        owner=message['from']['id']
        d=await self.db.by_thread(owner,message.get('message_thread_id'))
        mid=message.get('reply_to_message',{}).get('message_id')
        if not d or not mid:
            raise Rejected('Ответьте командой /delete на своё сообщение в теме MAX.')
        links=await self.db.links(d,'tg',mid)
        if not links or any(link['origin']!='tg' for link in links):
            raise Rejected('Удалять можно только собственные сообщения, отправленные этим мостом.')
        await self.db.enqueue(d,'max','delete',str(mid),'',[{}])

    async def topic(self,d):
        d=await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2',d['id'],d['owner'])
        if d['topic_state']=='ready' and d['tg_thread_id'] is not None:
            return d['tg_thread_id']
        if d['topic_state'] in ('creating','unknown'):
            raise Rejected(f"Результат создания темы неизвестен. Найдите тему и отправьте в ней /bind {d['id']}. Не создавайте её повторно вслепую.")
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
            raise Rejected('Команду /bind нужно отправить внутри существующей темы.')
        try:
            import uuid
            did=uuid.UUID(did)
        except ValueError:
            raise Rejected('Неверный идентификатор диалога.') from None
        async with self.db.pool.acquire() as c,c.transaction():
            d=await c.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2 FOR UPDATE',did,owner)
            if not d or d['topic_state'] not in ('unknown','pending'):
                raise Rejected('Диалог не найден или уже привязан.')
            if await c.fetchval('SELECT 1 FROM dialogs WHERE owner=$1 AND tg_thread_id=$2',owner,thread):
                raise Rejected('Эта тема уже используется другим диалогом.')
            await c.execute("UPDATE dialogs SET tg_thread_id=$3,topic_state='ready' WHERE id=$1 AND owner=$2",did,owner,thread)
        # Failed jobs remain paused until an explicit /retry; binding is not sending.

    async def recover_history(self,entry):
        a=await self.db.account(entry.account['owner'])
        if not a or entry.closing:
            return
        start=max(a['since_ms'],a['history_ms']-1000)
        end=int(time.time()*1000)
        chats={c.id:c for c in (entry.client.chats or []) if is_dialog(c)}
        marker=None
        # Bounded work per pass, but never advance the checkpoint on an incomplete pass.
        for _ in range(self.settings.history_pages):
            page=await entry.client.fetch_chats(marker=marker)
            if not page:
                break
            chats.update({c.id:c for c in page if is_dialog(c)})
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
                    if job['action']!='send':
                        await self.db.mark_sending(job)
                        external=True
                        await self.change(entry,d,job,p)
                        await self.db.complete(job)
                        continue
                    source='max' if job['direction']=='tg' else 'tg'
                    reply=None
                    if p.get('reply_to'):
                        links=await self.db.links(d,source,p['reply_to'])
                        if links:
                            reply=links[0]['tg_message_id' if source=='max' else 'max_message_id']
                    if job['direction']=='tg':
                        thread=await self.topic(d)
                        upload=None
                        kind='text'
                        if p.get('attachment'):
                            kind,name,raw=await self.media.for_telegram(entry.client,d['max_chat_id'],job['source_id'],p['attachment']['index'])
                            upload=(kind,name,raw)
                        await self.tg.pace(d['owner'])
                        await self.db.mark_sending(job)
                        external=True
                        params={'chat_id':d['owner'],'message_thread_id':thread}
                        if reply:
                            params['reply_parameters']={'message_id':int(reply),'allow_sending_without_reply':True}
                        if upload:
                            field,name,raw=upload
                            method={'photo':'sendPhoto','document':'sendDocument','video':'sendVideo','voice':'sendVoice'}[field]
                            result=await self.tg.call(method,params,files={field:(name,raw)})
                        else:
                            result=await self.tg.call('sendMessage',{**params,'text':p['text'],'link_preview_options':{'is_disabled':True}})
                        await self.db.complete(job,result['message_id'],job['source_id'],kind)
                    else:
                        attachments=[await self.media.for_max(p['attachment'])] if p.get('attachment') else None
                        await self.db.mark_sending(job)
                        external=True
                        result=await mutate(entry.client,'send_message',chat_id=d['max_chat_id'],text=p['text'] or None,reply_to=int(reply) if reply else None,attachments=attachments)
                        await self.db.complete(job,job['source_id'],result.id,p.get('attachment',{}).get('kind','text'))
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
                    await self.db.state(job,'pending','Тема удалена; будет создана заново.',2)
                elif job['direction']=='tg' and ('topic_closed' in description or 'topic is closed' in description):
                    with contextlib.suppress(Exception):
                        await self.tg.call('reopenForumTopic',{'chat_id':job['owner'],'message_thread_id':d['tg_thread_id']})
                    await self.db.state(job,'failed','Тема закрыта. Откройте её и повторите задание.')
                else:
                    await self.db.state(job,'failed',str(exc))
            except Rejected as exc:
                await self.db.state(job,'failed',str(exc))
            except Exception as exc:
                log.warning('Delivery failed (%s), job=%s',type(exc).__name__,job['id'])
                await self.db.state(job,'unknown' if external else 'failed','Не удалось подтвердить результат.' if external else 'Ошибка подготовки сообщения. Используйте /retry после проверки /status.')

    async def change(self,entry,d,job,p):
        source='max' if job['direction']=='tg' else 'tg'
        links=await self.db.links(d,source,job['source_id'])
        if not links:
            # An edit/delete for a pre-bridge message must not create a new message.
            return
        if job['action']=='delete':
            if source=='max':
                for link in links:
                    try:
                        await self.tg.call('deleteMessage',{'chat_id':d['owner'],'message_id':link['tg_message_id']})
                    except TelegramRejected as exc:
                        if 'message to delete not found' not in exc.description.lower():
                            raise
            else:
                if any(link['origin']!='tg' for link in links):
                    raise Rejected('Нельзя удалить чужое сообщение.')
                await mutate(entry.client,'delete_message',chat_id=d['max_chat_id'],message_ids=list({int(x['max_message_id']) for x in links}),for_me=False)
            return
        parts=p['parts']
        text_links=[x for x in links if x['kind']=='text']
        text_parts=[x for x in parts if not x.get('attachment')]
        if len(text_parts)!=len(text_links) or any(x.get('attachment') for x in parts):
            raise Rejected('Редактирование вложений или изменение числа частей текста пока не поддерживается. Отправьте новое сообщение.')
        for link,part in zip(text_links,text_parts):
            if source=='max':
                try:
                    await self.tg.call('editMessageText',{'chat_id':d['owner'],'message_id':link['tg_message_id'],'text':part['text'],'link_preview_options':{'is_disabled':True}})
                except TelegramRejected as exc:
                    if 'message is not modified' not in exc.description.lower():
                        raise
            else:
                if link['origin']!='tg':
                    raise Rejected('Нельзя редактировать чужое сообщение.')
                await mutate(entry.client,'edit_message',chat_id=d['max_chat_id'],message_id=int(link['max_message_id']),text=part['text'])

    async def notify_errors(self):
        rows=await self.db.pool.fetch("""SELECT j.*,d.tg_thread_id FROM jobs j JOIN dialogs d ON d.id=j.dialog_id
            WHERE j.status IN ('failed','unknown','skipped') AND j.error IS NOT NULL AND NOT j.notified
            ORDER BY j.id LIMIT 20""")
        for j in rows:
            text=f"Доставка #{j['id']}: {j['error']}\n/retry {j['id']} — повторить, /skip {j['id']} — пропустить."
            if j['status']=='unknown':
                text+='\nРезультат неизвестен. Проверьте MAX/Telegram; повтор может создать дубль.'
            try:
                await self.tg.text(j['owner'],text,j['tg_thread_id'])
            except TelegramRejected:
                try:
                    await self.tg.text(j['owner'],text)
                except Exception:
                    continue
            except Exception:
                continue
            await self.db.pool.execute('UPDATE jobs SET notified=true WHERE id=$1 AND owner=$2',j['id'],j['owner'])
