import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from maxost.auth import Authentication
from maxost.max_auth import InteractiveQrLogin
from maxost.max_client import Connection, MaxHub
from maxost.ui import Screen, card


def callback(s, key, qid='q'):
    return {
        'id': qid,
        'from': {'id': s.owner},
        'message': {
            'message_id': s.message_id,
            'chat': {'id': s.owner, 'type': 'private'},
        },
        'data': f'a:{s.nonce}:{s.phase}:{key}',
    }


def auth_fixture(hub):
    db = NS(
        account=AsyncMock(return_value=None),
        consent=AsyncMock(),
        new_account=AsyncMock(return_value={'id': 'a', 'owner': 101}),
        pool=NS(execute=AsyncMock(return_value='DELETE 1')),
    )
    tg = NS(
        call=AsyncMock(return_value={'message_id': 42}),
        photo=AsyncMock(return_value={'message_id': 77}),
        remove=AsyncMock(return_value=True),
    )
    auth = Authentication(
        db,
        tg,
        hub,
        NS(max_accounts=100, auth_ttl=600, rich_ui=True),
    )
    auth.refresh = Mock()
    return auth


@pytest.mark.asyncio
async def test_qr_flow_requests_displays_polls_confirms_and_returns_token():
    provider = NS(
        progress=AsyncMock(),
        show_qr=AsyncMock(),
        qr_confirmed=AsyncMock(),
        get_password=AsyncMock(return_value='unused'),
    )
    qr = NS(
        qr_link='https://max.ru/login/example',
        track_id='track-only',
        polling_interval=1,
        expires_at=int((time.time()+30)*1000),
    )
    api = NS(
        request_qr=AsyncMock(return_value=qr),
        check_qr=AsyncMock(
            return_value=NS(status=NS(login_available=True))
        ),
        confirm_qr=AsyncMock(
            return_value=NS(
                login_token='web-session',
                password_challenge=None,
            )
        ),
        check_password=AsyncMock(),
    )
    app = NS(api=NS(auth=api))

    result = await InteractiveQrLogin(provider).authenticate(app)

    assert result.token == 'web-session'
    api.request_qr.assert_awaited_once_with()
    provider.show_qr.assert_awaited_once_with(qr.qr_link)
    api.check_qr.assert_awaited_once_with('track-only')
    provider.qr_confirmed.assert_awaited_once_with()
    api.confirm_qr.assert_awaited_once_with('track-only')
    stages = [call.args[0] for call in provider.progress.await_args_list]
    assert stages == ['requesting_qr', 'waiting_qr', 'confirming_qr', 'login']


@pytest.mark.asyncio
async def test_qr_2fa_uses_same_private_password_provider():
    provider = NS(
        progress=AsyncMock(),
        show_qr=AsyncMock(),
        qr_confirmed=AsyncMock(),
        get_password=AsyncMock(return_value='2fa-secret'),
    )
    api = NS(
        request_qr=AsyncMock(return_value=NS(
            qr_link='https://max.ru/login/example',
            track_id='track',
            polling_interval=1,
            expires_at=int((time.time()+30)*1000),
        )),
        check_qr=AsyncMock(return_value=NS(status=NS(login_available=True))),
        confirm_qr=AsyncMock(return_value=NS(
            login_token=None,
            password_challenge=NS(track_id='2fa-track'),
        )),
        check_password=AsyncMock(return_value=NS(
            login_token='session-after-2fa',
            error=None,
        )),
    )
    result = await InteractiveQrLogin(provider).authenticate(NS(api=NS(auth=api)))
    assert result.token == 'session-after-2fa'
    api.check_password.assert_awaited_once_with('2fa-track', '2fa-secret')


def test_auth_ui_offers_qr_before_and_after_sms_request():
    phone = Screen(101, phase='phone', buffer='79990000101')
    code = Screen(
        101,
        phase='code',
        phone='+79990000101',
        code_length=6,
    )
    phone_json = json.dumps(card(phone), ensure_ascii=False)
    code_json = json.dumps(card(code), ensure_ascii=False)
    assert 'Войти по QR' in phone_json
    assert 'Код не пришёл — войти по QR' in code_json
    assert f'a:{phone.nonce}:phone:qr' in phone_json
    assert f'a:{code.nonce}:code:qr' in code_json


@pytest.mark.asyncio
async def test_qr_image_is_sent_as_protected_png_and_removed_after_confirmation():
    hub = NS(authenticate=AsyncMock())
    auth = auth_fixture(hub)
    s = Screen(101, phase='requesting', message_id=42)
    auth.screens[101] = s

    await auth.show_qr(s, 'https://max.ru/login/private-token')

    assert s.phase == 'qr' and s.qr_message_id == 77
    args = auth.tg.photo.await_args.args
    kwargs = auth.tg.photo.await_args.kwargs
    assert args[0] == 101
    assert args[1].startswith(b'\x89PNG\r\n\x1a\n')
    assert 'private-token' not in kwargs['caption']
    assert kwargs['reply_markup']['inline_keyboard'][0][0]['url'].endswith(
        '/private-token'
    )

    await auth.clear_qr(s)
    auth.tg.remove.assert_awaited_once_with(101, 77)
    assert s.qr_message_id is None


@pytest.mark.asyncio
async def test_sms_attempt_can_switch_to_qr_without_second_account_or_sms_request():
    kinds = []
    mobile_waiting = asyncio.Event()

    async def authenticate(account, provider):
        kinds.append(provider.client_kind)
        if provider.client_kind == 'mobile':
            await provider.code_requested(6)
            mobile_waiting.set()
            await provider.get_code('+79990000101')
        else:
            provider.input_ready.set()

    auth = auth_fixture(NS(authenticate=AsyncMock(side_effect=authenticate)))
    s = Screen(
        101,
        phase='phone',
        buffer='79990000101',
        message_id=42,
    )
    auth.screens[101] = s

    await auth.callback(callback(s, 'submit', 'sms'))
    await asyncio.wait_for(mobile_waiting.wait(), 1)
    assert s.phase == 'code'

    await auth.callback(callback(s, 'qr', 'switch'))
    await asyncio.wait_for(s.task, 1)

    assert kinds == ['mobile', 'web']
    auth.db.new_account.assert_awaited_once_with(101, '+79990000101', 100)
    assert s.phase == 'ready'


def test_hub_build_uses_webclient_for_persisted_qr_session():
    pytest.importorskip('pymax')
    db = NS(
        pool=NS(),
        vault=NS(open=Mock(return_value='+79990000101')),
    )
    hub = MaxHub(db, NS())
    provider = NS()
    web = hub.build(
        Connection({
            'id': 'a',
            'owner': 101,
            'phone_cipher': b'ignored',
            'client_kind': 'web',
        }),
        provider,
    )
    mobile = hub.build(
        Connection({
            'id': 'b',
            'owner': 102,
            'phone_cipher': b'encrypted',
            'client_kind': 'mobile',
        }),
        provider,
    )
    assert type(web).__name__ == 'WebClient'
    assert type(mobile).__name__ == 'Client'
    assert isinstance(web._auth_flow, InteractiveQrLogin)
