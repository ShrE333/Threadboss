from __future__ import annotations

import logging

from .agent_runtime import AgentRuntime
from .config import Settings
from .media_processor import MediaProcessingError, MediaProcessor
from .models import NormalizedMessage
from .redis_bus import EventBus
from .router import SelfChatRouter
from .waha import WahaClient

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, settings: Settings, bus: EventBus):
        self.settings = settings
        self.bus = bus
        self.waha = WahaClient(settings)
        self.runtime = AgentRuntime(settings)
        self.media = MediaProcessor(settings)
        self.router = SelfChatRouter(settings, bus, self.waha, self.runtime, self.media)

    async def process(self, message: NormalizedMessage) -> None:
        if not await self.bus.claim_event(message.event_id):
            logger.info('Duplicate event ignored: %s', message.event_id)
            return

        if message.is_self_chat:
            if (message.source or '').lower() == 'api':
                logger.info('Ignoring ThreadBoss-originated self-chat message: %s', message.message_id)
                return
            if message.has_media and message.media:
                await self.bus.remember_attachment(message.session_id, message.chat_id, message.media)
            await self.router.handle(message)
            return

        permissions = await self.runtime.db.get_tenant_permissions(message.tenant_id)
        if message.chat_type == 'group' and not permissions.get('read_group_messages', True):
            logger.info('Tenant permission skipped group message: %s', message.message_id)
            return
        if message.chat_type == 'dm' and not permissions.get('read_direct_messages', True):
            logger.info('Tenant permission skipped direct message: %s', message.message_id)
            return

        if message.has_media and message.media:
            mimetype = (message.media.mimetype or '').lower()
            media_allowed = True
            if mimetype.startswith('image/'):
                media_allowed = bool(permissions.get('process_images', True))
            elif mimetype.startswith('audio/') or mimetype.startswith('video/'):
                media_allowed = bool(permissions.get('process_voice_notes', True))
            else:
                media_allowed = bool(permissions.get('process_documents', True))

            if media_allowed:
                try:
                    # V1.7 passively indexes permitted image/document text locally so event posters,
                    # screenshots and PDFs become searchable memory. Audio remains opt-in.
                    if self.settings.enrich_normal_chat_media:
                        context = await self.media.context_for_media(message.media, message.body)
                    else:
                        context = await self.media.context_for_memory(message.media)
                    if context:
                        message.body = (message.body + '\n\n--- Media context ---\n' + context).strip()
                except MediaProcessingError as exc:
                    logger.warning('Could not enrich normal-chat media %s: %s', message.message_id, exc)
            else:
                logger.info('Tenant permission skipped media processing: %s', message.message_id)

        await self.runtime.ingest(message.knowledge_payload())
