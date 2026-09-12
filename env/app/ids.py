from __future__ import annotations


def normalize_jid(value: str | None) -> str:
    if not value:
        return ''
    value = value.strip()
    # WAHA docs note GOWS/NOWEB internals may expose @s.whatsapp.net,
    # while sending should use @c.us.
    if value.endswith('@s.whatsapp.net'):
        return value.removesuffix('@s.whatsapp.net') + '@c.us'
    return value


def infer_chat_type(chat_id: str, owner_id: str) -> str:
    chat_id = normalize_jid(chat_id)
    owner_id = normalize_jid(owner_id)
    if chat_id and chat_id == owner_id:
        return 'self'
    if chat_id.endswith('@g.us'):
        return 'group'
    if chat_id.endswith('@newsletter'):
        return 'channel'
    if chat_id == 'status@broadcast':
        return 'status'
    if chat_id.endswith('@c.us') or chat_id.endswith('@lid'):
        return 'dm'
    return 'unknown'
