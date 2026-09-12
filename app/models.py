from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class MediaRef(BaseModel):
    url: Optional[str] = None
    mimetype: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None


class NormalizedMessage(BaseModel):
    event_id: str
    tenant_id: str
    session_id: str
    owner_id: str
    owner_lid: Optional[str] = None
    message_id: str
    chat_id: str
    chat_type: Literal['self', 'dm', 'group', 'channel', 'status', 'unknown']
    is_self_chat: bool
    sender_id: Optional[str] = None
    recipient_id: Optional[str] = None
    participant_id: Optional[str] = None
    from_me: bool = False
    source: Optional[str] = None
    timestamp: datetime
    body: str = ''
    interactive_id: Optional[str] = None
    interactive_title: Optional[str] = None
    interactive_context_id: Optional[str] = None
    has_media: bool = False
    media: Optional[MediaRef] = None
    raw_engine: Optional[str] = None
    raw_event: str = 'message.any'

    def knowledge_payload(self) -> Dict[str, Any]:
        return {
            'event_id': self.event_id,
            'tenant_id': self.tenant_id,
            'channel': 'whatsapp',
            'session_id': self.session_id,
            'owner_id': self.owner_id,
            'chat_id': self.chat_id,
            'chat_type': self.chat_type,
            'sender_id': self.sender_id,
            'recipient_id': self.recipient_id,
            'participant_id': self.participant_id,
            'message_id': self.message_id,
            'timestamp': self.timestamp,
            'text': self.body,
            'from_me': self.from_me,
            'has_media': self.has_media,
            'media': self.media.model_dump() if self.media else None,
            'metadata': {'source': self.source, 'engine': self.raw_engine},
        }


class ChannelMessageRequest(BaseModel):
    """Common ingestion contract for WhatsApp, Slack and Telegram adapters.

    V1.7 does not trust tenant_id from the body when a tenant-scoped API token
    is used. The authenticated token determines the effective tenant.
    """
    tenant_id: Optional[str] = None
    channel: Literal['whatsapp', 'slack', 'telegram', 'other'] = 'other'
    session_id: str = 'default'
    chat_id: str
    chat_type: str = 'unknown'
    sender_id: Optional[str] = None
    recipient_id: Optional[str] = None
    participant_id: Optional[str] = None
    message_id: str
    timestamp: datetime
    text: str = ''
    from_me: bool = False
    has_media: bool = False
    media: Optional[MediaRef] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def payload(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        data = self.model_dump(mode='python')
        if tenant_id is not None:
            data['tenant_id'] = tenant_id
        if not data.get('tenant_id'):
            raise ValueError('tenant_id was not resolved')
        return data


class AgentQueryRequest(BaseModel):
    tenant_id: Optional[str] = None
    channel: Literal['whatsapp', 'slack', 'telegram', 'other'] = 'other'
    session_id: str = 'default'
    chat_id: str = 'control'
    question: str
    source: str = 'control_chat'
    attachment: Optional[MediaRef] = None


class AgentQueryResponse(BaseModel):
    answer: str
    agent: str
    sources: list[dict[str, Any]] = Field(default_factory=list)


KnowledgeQueryRequest = AgentQueryRequest
KnowledgeQueryResponse = AgentQueryResponse


class SessionBootstrapResponse(BaseModel):
    session: str
    tenant_id: str
    owner_id: str
    owner_lid: Optional[str] = None
    self_chat_id: str
    webhook_url: str
    webhook_configured: bool


class HealthResponse(BaseModel):
    ok: bool
    service: str
    environment: str
    time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------
# Website / multi-user onboarding contracts
# ---------------------------------------------------------------------
class RegisterUserRequest(BaseModel):
    auth_provider: str = Field(min_length=1, max_length=50)
    auth_subject: str = Field(min_length=1, max_length=255)
    email: Optional[str] = None
    display_name: Optional[str] = None
    tenant_name: Optional[str] = None


class RegisterUserResponse(BaseModel):
    user_id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    onboarding_completed: bool = False
    whatsapp_connection: Optional[dict[str, Any]] = None


class StartWhatsAppRequest(BaseModel):
    tenant_id: str


class StartWhatsAppResponse(BaseModel):
    connection_id: str
    tenant_id: str
    session: str
    status: str
    qr_endpoint: str
    status_endpoint: str


class WhatsAppStatusResponse(BaseModel):
    connection_id: str
    tenant_id: str
    session: str
    status: str
    waha_status: Optional[str] = None
    owner_id: Optional[str] = None
    owner_lid: Optional[str] = None
    self_chat_id: Optional[str] = None


class CreateChannelTokenRequest(BaseModel):
    tenant_id: str
    channel: Literal['any', 'slack', 'telegram', 'other'] = 'any'
    label: Optional[str] = None


class CreateChannelTokenResponse(BaseModel):
    tenant_id: str
    channel: str
    token: str


class TenantPermissionsUpdate(BaseModel):
    tenant_id: str
    read_direct_messages: Optional[bool] = None
    read_group_messages: Optional[bool] = None
    process_images: Optional[bool] = None
    process_voice_notes: Optional[bool] = None
    process_documents: Optional[bool] = None
    create_reminders: Optional[bool] = None
    calendar_read: Optional[bool] = None
    calendar_write: Optional[bool] = None
    send_messages: Optional[bool] = None
    auto_followups: Optional[bool] = None
    allow_actions: Optional[bool] = None
    history_import_hours: Optional[int] = Field(default=None, ge=0, le=24*30)
