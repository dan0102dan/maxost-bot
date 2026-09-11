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
