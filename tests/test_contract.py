"""Offline signature checks against the immutable PyMax revision used by Docker."""
import inspect
import pytest

pytestmark=pytest.mark.contract


def test_pymax_surface():
    pymax=pytest.importorskip('pymax')
    assert {'phone','sms_code_provider','password_provider','extra_config'} <= set(inspect.signature(pymax.Client).parameters)
    for method in ('connect','close','get_chat','get_user','get_message','get_file_by_id','get_video_by_id','fetch_history','fetch_chats','send_message','edit_message','delete_message','logout','on_message','on_message_edit','on_message_delete'):
        assert callable(getattr(pymax.Client,method))
    assert {'chat_id','text','reply_to','attachments'} <= set(inspect.signature(pymax.Client.send_message).parameters)
    assert {'message_ids','for_me'} <= set(inspect.signature(pymax.Client.delete_message).parameters)
    assert {'from_time','backward','forward'} <= set(inspect.signature(pymax.Client.fetch_history).parameters)
    config=pymax.ExtraConfig(reconnect=False,relogin=False,persist_session=True,password_max_attempts=3,log_level='CRITICAL')
    assert config.persist_session and not config.relogin


@pytest.mark.asyncio
async def test_pinned_raw_send_edit_payloads_and_response_envelopes():
    pytest.importorskip('pymax')
    from types import SimpleNamespace as NS
    from pymax.protocol import Opcode
    from maxost.max_transport import transmit
    calls = []
    async def invoke(opcode, payload):
        calls.append((opcode, payload))
        message = {'id': 91, 'time': 1, 'type': 'USER', 'text': '**literal**', 'attaches': []}
        return NS(payload=message if opcode == Opcode.MSG_SEND else {'message': message})
    service = NS(_next_cid=lambda: 42)
    app = NS(invoke=invoke, api=NS(messages=service))
    client = NS(_app=app)
    part = {'text': '**literal**', 'entities': [{'type': 'bold', 'offset': 2, 'length': 7}]}
    assert (await transmit(client, 5, part, [], reply='12'))['id'] == 91
    assert (await transmit(client, 5, part, [], message_id='91'))['id'] == 91
    sent, edited = calls[0][1], calls[1][1]
    assert sent['message']['text'] == '**literal**'
    assert sent['message']['elements'] == [{'type': 'STRONG', 'from': 2, 'length': 7}]
    assert sent['message']['link']['messageId'] == 12
    assert edited['messageId'] == 91 and edited['attachments'] == []
    assert edited['elements'] == sent['message']['elements']
    assert 42 in client._maxost_sent_cids


def test_pinned_content_and_reaction_contracts():
    pymax = pytest.importorskip('pymax')
    from pymax.types.domain.attachments.poll import Poll
    from pymax.types.domain.element import Element
    from maxost.formatting import from_max
    for name in ('add_reaction', 'remove_reaction', 'get_reactions', 'vote_poll', 'on_reaction_update'):
        assert callable(getattr(pymax.Client, name))
    assert {'text','attachments','message_id'} <= set(inspect.signature(pymax.Client.edit_message).parameters)
    assert {'poll_id','answer_ids'} <= set(inspect.signature(pymax.Client.vote_poll).parameters)
    poll = Poll(title='Choose', answers=[{'text': 'A'}, {'text': 'B'}], settings=7)
    assert int(poll.settings) == 7 and len(poll.answers) == 2
    entity = Element.model_validate({'type': 'STRONG', 'from': 2, 'length': 3})
    assert from_max('😀abc', [entity])[0] == [{'type': 'bold','offset':2,'length':3}]
