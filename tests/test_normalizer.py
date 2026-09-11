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
        tenant_id='tenant_shriram',
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
        tenant_id='tenant_shriram',
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
        tenant_id='tenant_shriram',
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
        tenant_id='tenant_shriram',
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
