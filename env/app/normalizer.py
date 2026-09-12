from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from .ids import infer_chat_type, normalize_jid
from .models import MediaRef, NormalizedMessage


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
    return False


def _chat_id_from_message_id(message_id: str) -> str:
    """Extract WAHA's chat JID from ids such as true_123@c.us_ABC.

    GOWS history rows can omit ``chatId`` and, for outgoing messages, may omit
    ``to`` too. The full WAHA message id still contains the chat id.
    """
    value = str(message_id or '')
    match = re.match(r'^(?:true|false)_([^_]+)_.+$', value, flags=re.I)
    if not match:
        return ''
    candidate = normalize_jid(match.group(1))
    if candidate.endswith(('@c.us', '@lid', '@g.us', '@newsletter')) or candidate == 'status@broadcast':
        return candidate
    return ''


def _derive_chat_id(payload: dict[str, Any], owner_id: str) -> str:
    explicit = payload.get('chatId')
    if explicit:
        return normalize_jid(str(explicit))

    # History API responses do not always include chatId/to. WAHA's message id
    # encodes the chat jid and is the safest fallback for outgoing history rows.
    from_id = _chat_id_from_message_id(str(payload.get('id') or ''))
    if from_id:
        return from_id

    sender = normalize_jid(payload.get('from'))
    recipient = normalize_jid(payload.get('to'))
    from_me = _as_bool(payload.get('fromMe'))

    for candidate in (sender, recipient):
        if candidate.endswith('@g.us') or candidate.endswith('@newsletter') or candidate == 'status@broadcast':
            return candidate

    if from_me:
        return recipient or sender or owner_id
    return sender or recipient or owner_id


def _timestamp(value: Any) -> datetime:
    try:
        numeric = float(value)
        # Be tolerant if WAHA changes an event timestamp to milliseconds.
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _deep_find(obj: Any, wanted: set[str]) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted and isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for value in obj.values():
            found = _deep_find(value, wanted)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep_find(value, wanted)
            if found:
                return found
    return None


def _extract_interactive_selection(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    interactive_id = _deep_find(
        payload,
        {
            'selectedRowId', 'selectedRowID', 'selectedId', 'selectedID',
            'rowId', 'rowID', 'buttonId', 'buttonID',
        },
    )
    interactive_title = _deep_find(
        payload,
        {
            'selectedDisplayText', 'selectedTitle', 'displayText', 'buttonText',
        },
    )
    context_id = _deep_find(
        payload,
        {'contextInfoId', 'stanzaId', 'quotedMessageId', 'replyTo'},
    )
    return interactive_id, interactive_title, context_id


def _is_self(owner_id: str, owner_lid: str | None, *candidates: str | None) -> bool:
    aliases = {normalize_jid(x) for x in (owner_id, owner_lid) if x}
    normalized = [normalize_jid(x) for x in candidates if x]
    return any(value in aliases for value in normalized)


def _media_ref(payload: dict[str, Any]) -> MediaRef | None:
    media_data = payload.get('media') if isinstance(payload.get('media'), dict) else None
    if media_data:
        return MediaRef.model_validate(media_data)

    # Some GOWS/history payloads expose mediaUrl rather than a full media object.
    media_url = payload.get('mediaUrl')
    if media_url:
        return MediaRef(
            url=str(media_url),
            mimetype=str(payload.get('mimetype') or payload.get('mediaType') or '') or None,
            filename=str(payload.get('filename') or '') or None,
        )
    return None


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

    owner_aliases = {normalize_jid(value) for value in (owner_id, owner_lid) if value}
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

    media = _media_ref(payload)
    source = payload.get('source') or event.get('source')
    event_id = f'{session}:{message_id}'
    interactive_id, interactive_title, interactive_context_id = _extract_interactive_selection(payload)

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
        from_me=_as_bool(payload.get('fromMe')),
        source=str(source) if source is not None else None,
        timestamp=_timestamp(payload.get('timestamp')),
        body=str(payload.get('body') or ''),
        interactive_id=interactive_id,
        interactive_title=interactive_title,
        interactive_context_id=interactive_context_id,
        has_media=_as_bool(payload.get('hasMedia')) or bool(media),
        media=media,
        raw_engine=event.get('engine'),
        raw_event=str(event.get('event') or 'message.any'),
    )


def normalize_waha_poll_vote_event(
    event: dict[str, Any],
    *,
    tenant_id: str,
    owner_id: str,
    owner_lid: str | None = None,
) -> NormalizedMessage:
    payload = event.get('payload') or {}
    vote = payload.get('vote') if isinstance(payload.get('vote'), dict) else {}
    poll = payload.get('poll') if isinstance(payload.get('poll'), dict) else {}
    session = str(event.get('session') or 'default')
    owner_id = normalize_jid(owner_id)
    owner_lid = normalize_jid(owner_lid) or None

    def jid(value: Any) -> str:
        if value in {None, '', 'me'}:
            return owner_lid or owner_id
        return normalize_jid(str(value))

    sender_id = jid(vote.get('from'))
    recipient_id = jid(vote.get('to'))
    poll_to = jid(poll.get('to'))
    chat_id = poll_to or recipient_id or sender_id or owner_lid or owner_id

    selected = vote.get('selectedOptions') if isinstance(vote.get('selectedOptions'), list) else []
    selected = [str(x) for x in selected if str(x).strip()]
    title = selected[-1] if selected else ''

    poll_id = str(poll.get('id') or '')
    vote_id = str(vote.get('id') or '')
    message_id = vote_id or ('pollvote_' + hashlib.sha256(
        f'{session}|{poll_id}|{title}|{vote.get("timestamp")}'.encode('utf-8')
    ).hexdigest()[:32])

    is_self_chat = _is_self(owner_id, owner_lid, chat_id, sender_id, recipient_id)

    return NormalizedMessage(
        event_id=f'{session}:{message_id}',
        tenant_id=tenant_id,
        session_id=session,
        owner_id=owner_id,
        owner_lid=owner_lid,
        message_id=message_id,
        chat_id=chat_id,
        chat_type='self' if is_self_chat else infer_chat_type(chat_id, owner_id),
        is_self_chat=is_self_chat,
        sender_id=sender_id or None,
        recipient_id=recipient_id or None,
        participant_id=None,
        from_me=_as_bool(vote.get('fromMe')),
        source='app',
        timestamp=_timestamp(vote.get('timestamp')),
        body=title,
        interactive_id=None,
        interactive_title=title or None,
        interactive_context_id=poll_id or None,
        has_media=False,
        media=None,
        raw_engine=event.get('engine'),
        raw_event=str(event.get('event') or 'poll.vote'),
    )
