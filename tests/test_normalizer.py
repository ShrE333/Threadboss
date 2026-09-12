from app.normalizer import normalize_waha_event


def test_self_chat_detected_when_owner_messages_self():
    event = {
        'event': 'message.any',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'id': 'ABC',
            'timestamp': 1789080000,
            'from': '919999999999@c.us',
            'to': '919999999999@c.us',
            'fromMe': True,
            'body': 'what did my boss say?',
            'source': 'app',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
    )
    assert msg.is_self_chat is True
    assert msg.chat_type == 'self'
    assert msg.chat_id == '919999999999@c.us'


def test_gows_lid_self_chat_is_detected():
    event = {
        'event': 'message.any',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'id': 'LID1',
            'timestamp': 1789080000,
            'chatId': '123456789012345@lid',
            'from': '919999999999@c.us',
            'to': '123456789012345@lid',
            'fromMe': True,
            'body': '/help',
            'source': 'app',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.is_self_chat is True
    assert msg.chat_type == 'self'
    assert msg.chat_id == '123456789012345@lid'
    assert msg.owner_lid == '123456789012345@lid'


def test_outgoing_message_to_other_lid_is_not_self_chat():
    event = {
        'event': 'message.any',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'id': 'OTHER1',
            'timestamp': 1789080000,
            'chatId': '555555555555@lid',
            'from': '919999999999@c.us',
            'to': '555555555555@lid',
            'fromMe': True,
            'body': 'hello friend',
            'source': 'app',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.is_self_chat is False
    assert msg.chat_type == 'dm'


def test_normal_dm_is_not_self_chat():
    event = {
        'event': 'message.any',
        'session': 'default',
        'payload': {
            'id': 'XYZ',
            'timestamp': 1789080000,
            'from': '918888888888@c.us',
            'to': '919999999999@c.us',
            'fromMe': False,
            'body': 'send report Friday',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.is_self_chat is False
    assert msg.chat_type == 'dm'
    assert msg.chat_id == '918888888888@c.us'


def test_gows_internal_jid_is_normalized():
    event = {
        'event': 'message.any',
        'session': 'default',
        'payload': {
            'id': '1',
            'from': '919999999999@s.whatsapp.net',
            'to': '919999999999@s.whatsapp.net',
            'fromMe': True,
            'body': 'hello',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='t',
        owner_id='919999999999@c.us',
    )
    assert msg.is_self_chat
    assert msg.chat_id == '919999999999@c.us'


def test_list_reply_row_id_is_extracted():
    event = {
        'event': 'message.any',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'id': 'LIST1',
            'timestamp': 1789080000,
            'chatId': '123456789012345@lid',
            'from': '919999999999@c.us',
            'to': '123456789012345@lid',
            'fromMe': True,
            'body': '🛠 Tools',
            'source': 'app',
            '_data': {
                'listResponse': {
                    'singleSelectReply': {'selectedRowId': 'tb_home_tools'}
                }
            },
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.interactive_id == 'tb_home_tools'
    assert msg.is_self_chat is True


def test_poll_vote_normalizes_to_self_chat_selection():
    from app.normalizer import normalize_waha_poll_vote_event

    event = {
        'event': 'poll.vote',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'vote': {
                'id': 'VOTE1',
                'to': 'me',
                'from': '919999999999@c.us',
                'fromMe': True,
                'selectedOptions': ['🤖 Agents'],
                'timestamp': 1789080000,
            },
            'poll': {
                'id': 'POLL1',
                'to': '123456789012345@lid',
                'from': 'me',
                'fromMe': True,
            },
        },
    }
    msg = normalize_waha_poll_vote_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.is_self_chat is True
    assert msg.interactive_title == '🤖 Agents'
    assert msg.interactive_context_id == 'POLL1'


def test_history_outgoing_dm_without_to_uses_message_id_chat_jid():
    event = {
        'event': 'message.any',
        'session': 'default',
        'engine': 'GOWS',
        'payload': {
            'id': 'true_918888888888@c.us_3EB0ABC',
            'timestamp': 1789080000,
            'from': '919999999999@c.us',
            'fromMe': True,
            'body': 'sent to friend',
        },
    }
    msg = normalize_waha_event(
        event,
        tenant_id='tenant_demo',
        owner_id='919999999999@c.us',
        owner_lid='123456789012345@lid',
    )
    assert msg.chat_id == '918888888888@c.us'
    assert msg.is_self_chat is False
    assert msg.chat_type == 'dm'


def test_string_false_is_not_treated_as_true():
    event = {
        'event': 'message.any',
        'session': 'default',
        'payload': {
            'id': 'false_918888888888@c.us_ABC',
            'timestamp': 1789080000,
            'from': '918888888888@c.us',
            'fromMe': 'false',
            'body': 'incoming',
        },
    }
    msg = normalize_waha_event(event, tenant_id='t', owner_id='919999999999@c.us')
    assert msg.from_me is False
    assert msg.is_self_chat is False
