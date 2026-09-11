"""No real accounts or SMS: exercise the UI, lifecycle and pinned SDK together."""
import asyncio
import base64
import json
import logging
import os
import socket
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from maxost.auth import Authentication, Providers
from maxost.auth_diagnostics import describe_failure, safe_error_code
from maxost.crypto import Vault
from maxost.errors import Rejected
from maxost.max_auth import InteractiveLogin
from maxost.max_client import Connection, MaxHub, NoInteractiveLogin, SessionRevoked
from maxost.ui import Screen, apply_key, card


def callback(s, key, qid='q'):
    return {'id': qid, 'from': {'id': s.owner},
            'message': {'message_id': s.message_id, 'chat': {'id': s.owner, 'type': 'private'}},
            'data': f'a:{s.nonce}:{s.phase}:{key}'}


def auth_fixture(hub=None):
    db = NS(account=AsyncMock(return_value=None), consent=AsyncMock(),
            new_account=AsyncMock(return_value={'id': 'a', 'owner': 101}),
            pool=NS(execute=AsyncMock(return_value='DELETE 1')))
    tg = NS(call=AsyncMock(return_value={'message_id': 42}), remove=AsyncMock())
    auth = Authentication(db, tg, hub or NS(authenticate=AsyncMock()),
                          NS(max_accounts=100, auth_ttl=600, rich_ui=True))
    auth.refresh = Mock()  # Render is exercised explicitly; avoid timer-based tests.
    return auth


@pytest.mark.asyncio
async def test_phone_keypad_requests_once_and_code_goes_to_same_attempt():
    accepted = asyncio.Event()
    received = []
    async def login(account, provider):
        await provider.progress('requesting_code')
        await provider.code_requested(5)
        accepted.set()
        received.append(await provider.get_code('+79990000101'))
    hub = NS(authenticate=AsyncMock(side_effect=login))
    auth = auth_fixture(hub)
    await auth.start(101)
    s = auth.screens[101]
    await auth.callback(callback(s, 'accept', 'consent'))
    for n, digit in enumerate('79990000101'):
        await auth.callback(callback(s, digit, f'phone{n}'))
    hub.authenticate.assert_not_awaited()
    submit = callback(s, 'submit', 'submit')
    await auth.callback(submit)
    await auth.callback(submit)
    await asyncio.wait_for(accepted.wait(), 1)
    # Provider transitions before its first await of Telegram rendering.
    assert s.phase == 'code' and s.code_length == 5
    assert 'MAX принял запрос' in s.notice
    auth.db.new_account.assert_awaited_once_with(101, '+79990000101', 100)
    for n, digit in enumerate('1234'):
        await auth.callback(callback(s, digit, f'code{n}'))
    with pytest.raises(Rejected):
        await auth.callback(callback(s, 'submit', 'short'))
    await auth.callback(callback(s, '5', 'last'))
    await auth.callback(callback(s, 'submit', 'verify'))
    await asyncio.wait_for(s.task, 1)
    assert received == ['12345'] and s.phase == 'ready'
    assert s.buffer == s.phone == '' and s.future is None
    hub.authenticate.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_failure_is_visible_and_does_not_display_code_keypad(caplog):
    class ApiError(Exception):
        error = 'auth.limit.exceeded'
    secret = '+79990000101 token=private-token code=987654'
    auth = auth_fixture()
    async def reject(account, provider):
        await provider.progress('requesting_code')
        raise ApiError(secret)
    auth.hub.authenticate.side_effect = reject
    s = Screen(101, phase='requesting', phone='+79990000101', message_id=42)
    auth.screens[101] = s
    with caplog.at_level(logging.INFO, logger='maxost.auth'):
        await auth._login(s)
    assert s.phase == 'error' and 'RATE_LIMIT' in s.notice
    assert 'auth.limit.exceeded' in s.notice
    assert 'MAX принял запрос' not in s.notice
    assert secret not in caplog.text + s.notice
    assert 'private-token' not in caplog.text + s.notice
    assert '+79990000101' not in caplog.text + s.notice
    assert s.trace_id in caplog.text and s.trace_id in s.notice
    auth.db.pool.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_waiting_login_wipes_future_and_does_not_send_more_requests():
    entered = asyncio.Event()
    async def login(account, provider):
        await provider.code_requested(6)
        entered.set()
        await provider.get_code('')
    auth = auth_fixture(NS(authenticate=AsyncMock(side_effect=login)))
    s = Screen(101, phase='phone', buffer='79990000101', message_id=42)
    auth.screens[101] = s
    await auth.callback(callback(s, 'submit'))
    await asyncio.wait_for(entered.wait(), 1)
    future = s.future
    await asyncio.wait_for(auth.cancel(101), 1)
    assert 101 not in auth.screens
    assert future.cancelled()
    assert not s.phone and not s.buffer
    auth.hub.authenticate.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_failure_cannot_leave_an_unbounded_waiter():
    auth = auth_fixture()
    auth.tg.call.side_effect = RuntimeError('UI failed')
    s = Screen(101, phase='requesting')
    auth.screens[101] = s
    provider = Providers(auth, s)
    with pytest.raises(RuntimeError):
        await provider.get_code('')
    assert s.future is None and not provider.input_ready.is_set()


@pytest.mark.parametrize('length', [4, 5, 6, 8, 12])
def test_keypad_uses_server_length_and_never_renders_code(length):
    s = Screen(101, phase='code', code_length=length)
    for _ in range(20):
        apply_key(s, '9')
    assert len(s.buffer) == length
    rendered = json.dumps(card(s), ensure_ascii=False)
    assert '9' * length not in rendered


@pytest.mark.parametrize('value', ['+79990000101', 'token=abc', 'https://x/token', 'A'*100, 'FAIL_123456', {'token': 'x'}, None])
def test_diagnostic_codes_do_not_expose_arbitrary_data(value):
    exc = RuntimeError('do not log me')
    exc.error = value
    assert safe_error_code(exc) == 'UNCLASSIFIED'


def test_dns_and_timeout_stages_are_not_reported_as_wrong_sms():
    error = ConnectionError('private transport detail')
    error.__cause__ = socket.gaierror('private dns detail')
    assert describe_failure(error, 'connecting')[0] == 'DNS'
    assert describe_failure(TimeoutError(), 'waiting_code')[0] == 'INPUT_TIMEOUT'
    assert describe_failure(TimeoutError(), 'requesting_code')[0] == 'NETWORK_TIMEOUT'


@pytest.mark.asyncio
async def test_client_construction_failure_cleans_registry():
    hub = MaxHub(NS(), NS())
    hub.build = Mock(side_effect=ValueError('bad configuration'))
    with pytest.raises(ValueError):
        await hub.authenticate({'id': 'a', 'owner': 101}, NS())
    assert hub.connections == {}


@pytest.mark.asyncio
async def test_noninteractive_flow_never_requests_sms():
    app = NS(api=NS(auth=NS(request_code=AsyncMock())))
    with pytest.raises(SessionRevoked):
        await NoInteractiveLogin().authenticate(app)
    app.api.auth.request_code.assert_not_awaited()


def sdk_app(*, length=6, login_token='issued-session', challenge=None):
    pytest.importorskip('pymax')
    response = NS(login_token=login_token, password_challenge=challenge, register_token=None)
    api = NS(request_code=AsyncMock(return_value=NS(token='challenge-only', code_length=length)),
             send_code=AsyncMock(return_value=response),
             check_password=AsyncMock(return_value=NS(login_token='issued-session', error=None)))
    return NS(config=NS(phone='+79990000101'), api=NS(auth=api))


def fake_provider(code='123456'):
    return NS(progress=AsyncMock(), code_requested=AsyncMock(),
              get_code=AsyncMock(return_value=code), get_password=AsyncMock(return_value='test-password'))


@pytest.mark.asyncio
async def test_sdk_flow_calls_request_before_waiting_and_verifies_same_challenge():
    app, provider = sdk_app(length=5), fake_provider('12345')
    events = []
    async def request(phone):
        events.append('request')
        return NS(token='challenge-only', code_length=5)
    async def get_code(phone):
        events.append('input')
        return '12345'
    app.api.auth.request_code.side_effect = request
    provider.get_code.side_effect = get_code
    result = await InteractiveLogin(provider).authenticate(app)
    assert events == ['request', 'input'] and result.token == 'issued-session'
    app.api.auth.request_code.assert_awaited_once_with('+79990000101')
    app.api.auth.send_code.assert_awaited_once_with('challenge-only', '12345')
    provider.code_requested.assert_awaited_once_with(5)
    app.api.auth.check_password.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdk_flow_2fa_does_not_request_another_sms():
    app = sdk_app(login_token=None, challenge=NS(track_id='track-only'))
    provider = fake_provider()
    result = await InteractiveLogin(provider).authenticate(app)
    assert result.token == 'issued-session'
    app.api.auth.check_password.assert_awaited_once_with('track-only', 'test-password')
    app.api.auth.request_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_flow_network_timeout_never_opens_code_prompt():
    app, provider = sdk_app(), fake_provider()
    async def never(phone):
        await asyncio.Event().wait()
    app.api.auth.request_code.side_effect = never
    with pytest.raises(TimeoutError):
        await InteractiveLogin(provider, timeout=0.01).authenticate(app)
    provider.get_code.assert_not_awaited()
    provider.code_requested.assert_not_awaited()
    app.api.auth.request_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_pymax_runtime_connect_with_mocked_network_requests_sms():
    """Construct actual Client/App/ExtraConfig/Store; only network responses are fake."""
    pytest.importorskip('pymax')
    vault = Vault(base64.urlsafe_b64encode(os.urandom(32)).decode())
    account = {'id': 'a', 'owner': 101, 'max_user_id': None,
               'phone_cipher': vault.seal(101, 'phone:a', '+79990000101')}
    pool = NS(fetchval=AsyncMock(return_value=None), execute=AsyncMock(return_value='UPDATE 1'))
    db = NS(vault=vault, pool=pool)
    hub = MaxHub(db, NS())
    provider = fake_provider()
    client = hub.build(Connection(account), provider)
    try:
        await client._ensure_runtime()  # No open(), login or AUTH_REQUEST here.
        app = client._app
        assert isinstance(app.auth_flow, InteractiveLogin)
        assert app.config.relogin is False
        client._ensure_runtime = AsyncMock()  # Keep this prepared runtime.
        app.connection.open = AsyncMock()
        app.handshake = AsyncMock(return_value=NS(calls_seed=1))
        fake = sdk_app()
        app.api.auth.request_code = fake.api.auth.request_code
        app.api.auth.send_code = fake.api.auth.send_code
        profile = NS(contact=NS(id=10100))
        app.login = AsyncMock(return_value=(NS(profile=profile, token=None, chats=[],
                           contacts=[], messages={}, login2_flags=None), None))
        await client.connect()
        assert client.me is profile and app.started
        app.handshake.assert_awaited_once()
        app.api.auth.request_code.assert_awaited_once_with('+79990000101')
        app.api.auth.send_code.assert_awaited_once_with('challenge-only', '123456')
        pool.execute.assert_awaited_once()
        # Stored data is an encrypted session, not a code/challenge/password.
        stored = vault.open(101, 'session:a', pool.execute.call_args.args[3])
        assert stored['token'] == 'issued-session'
        assert '123456' not in json.dumps(stored)
        assert 'challenge-only' not in json.dumps(stored)
    finally:
        await client.close()
