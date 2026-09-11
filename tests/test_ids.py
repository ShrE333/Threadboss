from app.ids import infer_chat_type, normalize_jid


def test_normalize_jid():
    assert normalize_jid('123@s.whatsapp.net') == '123@c.us'
    assert normalize_jid('123@c.us') == '123@c.us'


def test_chat_types():
    assert infer_chat_type('123@c.us', '999@c.us') == 'dm'
    assert infer_chat_type('123@g.us', '999@c.us') == 'group'
    assert infer_chat_type('999@c.us', '999@c.us') == 'self'
