from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .ids import infer_chat_type, normalize_jid
from .models import MediaRef, NormalizedMessage


def _derive_chat_id(payload: dict[str, Any], owner_id: str) -> str:
    explicit = payload.get('chatId')
    if explicit:
        return normalize_jid(str(explicit))

    sender = normalize_jid(payload.get('from'))
    recipient = normalize_jid(payload.get('to'))
    from_me = bool(payload.get('fromMe'))

    # For groups, the group ID is normally in from/to depending on direction.
    for candidate in (sender, recipient):
        if candidate.endswith('@g.us') or candidate.endswith('@newsletter') or candidate == 'status@broadcast':
            return candidate

    if from_me:
        return recipient or sender or owner_id
    return sender or recipient or owner_id


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def normalize_waha_event(
    event: dict[str, Any],
    *,
    tenant_id: str,
    owner_id: str,
    owner_lid: str | None = None,
) -> NormalizedMessage:
    payload = event.get('payload') or {}
    session = str(event.get('session') or 'default')
    owner_id = normalize_jid(owner_id)
    owner_lid = normalize_jid(owner_lid) or None

    chat_id = _derive_chat_id(payload, owner_id)
    sender_id = normalize_jid(payload.get('from')) or None
    recipient_id = normalize_jid(payload.get('to')) or None
    participant_id = normalize_jid(payload.get('participant')) or None

    message_id = str(payload.get('id') or '')
    if not message_id:
        basis = '|'.join([
            session,
            chat_id,
            str(payload.get('timestamp') or ''),
            str(payload.get('body') or ''),
            sender_id or '',
        ])
        message_id = 'synthetic_' + hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]

    # GOWS can represent the same WhatsApp account in two forms:
    #   phone-number JID: 9199...@c.us
    #   linked ID:        1234...@lid
    # Treat BOTH as aliases of the owner when deciding whether a message came
    # from WhatsApp's "Message Yourself" chat.
    owner_aliases = {
        normalize_jid(value)
        for value in (owner_id, owner_lid)
        if value
    }

    normalized_chat = normalize_jid(chat_id)
    normalized_sender = normalize_jid(sender_id)
    normalized_recipient = normalize_jid(recipient_id)

    is_self_chat = (
        normalized_chat in owner_aliases
        or (
            bool(normalized_sender)
            and bool(normalized_recipient)
            and normalized_sender in owner_aliases
            and normalized_recipient in owner_aliases
        )
    )

    media_data = payload.get('media') if isinstance(payload.get('media'), dict) else None
    media = MediaRef.model_validate(media_data) if media_data else None

    source = payload.get('source') or event.get('source')
    event_id = f'{session}:{message_id}'

    return NormalizedMessage(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session,
        owner_id=owner_id,
        owner_lid=owner_lid,
        message_id=message_id,
        chat_id=chat_id,
        chat_type='self' if is_self_chat else infer_chat_type(chat_id, owner_id),
        is_self_chat=is_self_chat,
        sender_id=sender_id,
        recipient_id=recipient_id,
        participant_id=participant_id,
        from_me=bool(payload.get('fromMe')),
        source=str(source) if source is not None else None,
        timestamp=_timestamp(payload.get('timestamp')),
        body=str(payload.get('body') or ''),
        has_media=bool(payload.get('hasMedia') or media),
        media=media,
        raw_engine=event.get('engine'),
        raw_event=str(event.get('event') or 'message.any'),
    )
