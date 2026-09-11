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

        if self.settings.enrich_normal_chat_media and message.has_media and message.media:
            try:
                context = await self.media.context_for_media(message.media, message.body)
                if context:
                    message.body = (message.body + '\n\n--- Media context ---\n' + context).strip()
            except MediaProcessingError as exc:
                logger.warning('Could not enrich normal-chat media %s: %s', message.message_id, exc)

        await self.runtime.ingest(message.knowledge_payload())
