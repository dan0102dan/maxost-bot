import asyncio
import base64
import json
import os
from types import SimpleNamespace as NS

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from maxost.bridge import millis, normalize_max
from maxost.content import from_telegram, is_dialog, is_private_update, safe_name, split_text
from maxost.crypto import Vault
from maxost.errors import Rejected, RetryLater, Uncertain
from maxost.media import PublicResolver, validate_url
from maxost.telegram import Telegram
from maxost.ui import Screen, apply_key, card, fallback, valid_callback, validate_phone


def key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_encrypted_envelope_roundtrip_and_tenant_binding():
    v=Vault(key())
    secret={'token':'never-log-this','text':'Привет 😀'}
    blob=v.seal(101,'session:a',secret)
    assert b'never-log-this' not in blob
    assert v.open(101,'session:a',blob)==secret
    with pytest.raises(InvalidTag):
        v.open(102,'session:a',blob)
    with pytest.raises(InvalidTag):
        v.open(101,'job:a',blob)
    with pytest.raises((InvalidTag,ValueError)):
        v.open(101,'session:a',blob[:-1]+bytes([blob[-1]^1]))


def test_key_rotation_reads_old_data_and_writes_new():
    old,new=key(),key()
    blob=Vault(old).seal(1,'s',{'x':1})
    rotating=Vault(new+','+old)
    assert rotating.open(1,'s',blob)=={'x':1}
    new_blob=rotating.seal(1,'s',{'x':2})
    assert Vault(new).open(1,'s',new_blob)=={'x':2}
    assert rotating.digest('phone:+123')==Vault(old).digest('phone:+123')


@pytest.mark.parametrize('text', ['', 'a'*8000, '😀'*4097,'Привет\n'*1000, 'a😀z'*3000])
def test_split_preserves_unicode_exactly(text):
    parts=split_text(text)
    assert ''.join(parts)==text
    assert all(len(p.encode('utf-16-le'))//2<=3500 for p in parts)


@pytest.mark.parametrize('phone',['79991234567','375291234567','12025550123'])
def test_phone_valid(phone):
    assert validate_phone(phone)=='+'+phone


@pytest.mark.parametrize('phone',['','0'*10,'123','７９９９１２３４５６７','+79991234567','9'*16,'1234 5678'])
def test_phone_invalid(phone):
    with pytest.raises(Rejected):
        validate_phone(phone)


def test_keypad_masks_code_and_has_three_column_rows():
    s=Screen(10,phase='code',phone='+79991234567',buffer='123456')
    rich=card(s)
    text=json.dumps(rich,ensure_ascii=False)
    assert '123456' not in text and '+79991234567' not in text
    rows=[b for b in rich['blocks'] if b['type']=='buttons']
    assert [len(r['buttons']) for r in rows[:4]]==[3,3,3,3]
    assert all(len(b['callback_data'].encode())<=64 for r in rows for b in r['buttons'])
    html,markup=fallback(rich)
    assert '<pre>' in html and markup['inline_keyboard']


def test_keypad_reducer_limits_and_backspace():
    s=Screen(1,phase='phone')
    for _ in range(30):
        apply_key(s,'7')
    assert s.buffer=='7'*15
    apply_key(s,'back')
    assert len(s.buffer)==14
    apply_key(s,'clear')
    assert s.buffer==''
    apply_key(s,'submit')
    assert s.phase=='phone'


def query(s,**override):
    result={'id':'q1','from':{'id':s.owner},'message':{'message_id':s.message_id,'chat':{'id':s.owner,'type':'private'}},'data':f'a:{s.nonce}:{s.phase}:5'}
    return {**result,**override}


def test_callbacks_reject_other_owner_stale_message_and_duplicate():
    s=Screen(10,phase='code',message_id=20)
    assert valid_callback(s,query(s,**{'from':{'id':11}})) is None
    assert valid_callback(s,query(s,message={'message_id':19,'chat':{'id':10,'type':'private'}})) is None
    assert valid_callback(s,query(s,data=f'a:{s.nonce}:phone:5')) is None
    assert valid_callback(s,query(s))=='5'
    assert valid_callback(s,query(s)) is None
    s.expires=0
    assert valid_callback(s,query(s,id='q2')) is None


def test_scope_check_and_dialog_type():
    assert is_private_update({'chat':{'id':5,'type':'private'},'from':{'id':5}})
    assert not is_private_update({'chat':{'id':6,'type':'private'},'from':{'id':5}})
    assert not is_private_update({'chat':{'id':5,'type':'group'},'from':{'id':5}})
    assert is_dialog(NS(type='DIALOG'))
    assert not is_dialog(NS(type='CHAT'))


def test_caption_attachment_and_reply_are_not_lost():
    parts=from_telegram({'caption':'long caption','document':{'file_id':'id','file_name':'../../private.txt','file_size':10},'reply_to_message':{'message_id':8}},1024)
    assert parts[0]['text']=='long caption'
    assert parts[1]['attachment']['name']=='private.txt'
    assert all(p['reply_to']==8 for p in parts)
    with pytest.raises(Rejected):
        from_telegram({'document':{'file_id':'id','file_size':2048}},1024)
    with pytest.raises(Rejected):
        from_telegram({'sticker':{'file_id':'id'}},1024)
    assert safe_name('C:\\temp\\test.txt')=='test.txt'


def test_max_normalization_does_not_copy_self_destruct_content():
    assert 'SECRET' not in str(normalize_max(NS(ttl=True,text='SECRET')))
    msg=NS(ttl=False,text='Hello',attaches=[NS(type='PHOTO'),NS(type='STICKER')],link=None)
    parts=normalize_max(msg)
    assert parts[0]['text']=='Hello'
    assert parts[1]['attachment']['index']==0
    assert 'STICKER' in parts[2]['text']
    assert millis(1700000000)==1700000000000
    assert millis(1700000000000)==1700000000000


@pytest.mark.parametrize('url',['http://example.com/x','https://127.0.0.1/x','https://169.254.169.254/x','https://[::1]/x','https://localhost/x','https://user:pass@example.com/x','file:///etc/passwd','https://example.com:8443/x'])
def test_media_ssrf_url_rejection(url):
    with pytest.raises(Rejected):
        validate_url(url)


def test_media_public_url():
    assert validate_url('https://example.com/photo.jpg')=='https://example.com/photo.jpg'


@pytest.mark.asyncio
async def test_resolver_rejects_private_dns_result(monkeypatch):
    r=PublicResolver()
    async def resolve(*args):
        return [{'host':'10.0.0.8'}]
    monkeypatch.setattr(r.resolver,'resolve',resolve)
    with pytest.raises(OSError):
        await r.resolve('public-name.example',443)
    await r.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,exception',[('sendMessage',Uncertain),('createForumTopic',Uncertain),('getUpdates',RetryLater),('editMessageText',RetryLater)])
async def test_telegram_read_timeout_distinguishes_side_effects(method,exception):
    async def handler(request):
        raise httpx.ReadTimeout('token-secret-must-not-leak',request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        t=Telegram('123:secret',client=http)
        with pytest.raises(exception) as caught:
            await t.call(method,{'chat_id':1,'text':'secret'})
        assert 'token-secret' not in str(caught.value)


@pytest.mark.asyncio
async def test_telegram_rate_limit_is_retryable_and_has_delay():
    async def handler(request):
        return httpx.Response(429,json={'ok':False,'error_code':429,'parameters':{'retry_after':17}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RetryLater) as caught:
            await Telegram('secret',client=http).call('sendMessage')
        assert caught.value.delay==17


@pytest.mark.asyncio
async def test_telegram_private_topic_and_rich_payload_roundtrip():
    seen=[]
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'ok':True,'result':{'message_id':7}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        t=Telegram('secret',client=http)
        result=await t.text(123,'Hello',thread=456,reply=789)
        assert result['message_id']==7
        assert seen[0]['chat_id']==123 and seen[0]['message_thread_id']==456
        assert seen[0]['reply_parameters']['message_id']==789
        await t.call('sendRichMessage',{'chat_id':123,'rich_message':card(Screen(123))})
        assert seen[1]['rich_message']['blocks'][0]['text']=='MAXOST'


@pytest.mark.asyncio
async def test_auth_providers_have_no_console_or_persistence():
    from maxost.auth import Providers
    s=Screen(10,phase='requesting')
    class Auth:
        screens={10:s}
        async def render(self,_):
            pass
    p=Providers(Auth(),s)
    task=asyncio.create_task(p.get_code('+79991234567'))
    await asyncio.sleep(0)
    assert s.phase=='code' and s.future
    s.future.set_result('654321')
    assert await task=='654321'
    assert s.buffer=='' and s.future is None
