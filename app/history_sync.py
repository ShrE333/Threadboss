from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .agent_runtime import AgentRuntime
from .config import Settings
from .media_processor import MediaProcessingError, MediaProcessor
from .normalizer import normalize_waha_event
from .waha import WahaClient

logger = logging.getLogger(__name__)


class HistorySyncService:
    """Backfill recent GOWS WhatsApp history into ThreadBoss memory."""

    def __init__(
        self,
        settings: Settings,
        waha: WahaClient,
        runtime: AgentRuntime,
        media: MediaProcessor,
    ):
        self.settings = settings
        self.waha = waha
        self.runtime = runtime
        self.media = media

    async def sync(
        self,
        *,
        session: str,
        tenant_id: str,
        owner_id: str,
        owner_lid: str | None,
        hours: int,
        max_messages: int | None = None,
    ) -> dict[str, int]:
        hours = max(1, min(int(hours), 24 * 30))
        max_messages = max_messages or self.settings.initial_backfill_max_messages
        max_messages = max(1, int(max_messages))
        since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
        page_size = max(10, min(int(self.settings.history_sync_page_size), 200))

        fetched = 0
        ingested = 0
        skipped_self = 0
        media_enriched = 0
        offset = 0

        while fetched < max_messages:
            limit = min(page_size, max_messages - fetched)
            rows = await self.waha.get_messages_since(
                session,
                since,
                limit=limit,
                offset=offset,
                download_media=True,
            )
            if not rows:
                break

            for raw in rows:
                fetched += 1
                event = {
                    'session': session,
                    'event': 'message.any',
                    'engine': 'GOWS',
                    'payload': raw,
                }
                message = normalize_waha_event(
                    event,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    owner_lid=owner_lid,
                )
                if message.is_self_chat:
                    skipped_self += 1
                    continue

                if message.has_media and message.media:
                    try:
                        context = await self.media.context_for_memory(message.media)
                        if context:
                            message.body = (message.body + '\n\n--- Media context ---\n' + context).strip()
                            media_enriched += 1
                    except MediaProcessingError as exc:
                        logger.warning('History media extraction failed for %s: %s', message.message_id, exc)

                # Do not create empty memory records from protocol/system events.
                if not message.body.strip():
                    continue
                if await self.runtime.ingest(message.knowledge_payload()):
                    ingested += 1

            offset += len(rows)
            if len(rows) < limit:
                break

        return {
            'hours': hours,
            'fetched': fetched,
            'ingested': ingested,
            'skipped_self': skipped_self,
            'media_enriched': media_enriched,
        }
