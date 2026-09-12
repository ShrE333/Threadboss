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
    """Backfill GOWS WhatsApp history into ThreadBoss memory."""

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
    ) -> dict[str, int | str]:
        hours = max(1, min(int(hours), 24 * 30))
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        result = await self.sync_range(
            session=session,
            tenant_id=tenant_id,
            owner_id=owner_id,
            owner_lid=owner_lid,
            start=start,
            end=end,
            max_messages=max_messages,
        )
        result['hours'] = hours
        return result

    async def sync_range(
        self,
        *,
        session: str,
        tenant_id: str,
        owner_id: str,
        owner_lid: str | None,
        start: datetime,
        end: datetime,
        max_messages: int | None = None,
    ) -> dict[str, int | str]:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            raise ValueError('History sync end must be after start')

        max_messages = max_messages or self.settings.initial_backfill_max_messages
        max_messages = max(1, int(max_messages))
        page_size = max(10, min(int(self.settings.history_sync_page_size), 200))

        since = int(start.timestamp())
        # WAHA lte is inclusive while ThreadBoss query windows are [start, end).
        until = max(since, int(end.timestamp()) - 1)

        stats: dict[str, int | str] = {
            'fetched': 0,
            'inserted': 0,
            'repaired': 0,
            'duplicates': 0,
            'skipped_self': 0,
            'empty': 0,
            'media_enriched': 0,
            'failed': 0,
            'start': start.isoformat(),
            'end': end.isoformat(),
        }

        offset = 0
        seen_raw_ids: set[str] = set()
        previous_signature: tuple[str, ...] | None = None

        while int(stats['fetched']) < max_messages:
            limit = min(page_size, max_messages - int(stats['fetched']))
            rows = await self.waha.get_messages_range(
                session,
                since,
                until,
                limit=limit,
                offset=offset,
                download_media=True,
            )
            if not rows:
                break

            signature = tuple(str(r.get('id') or '') for r in rows[:5])
            if previous_signature is not None and signature == previous_signature and offset > 0:
                logger.warning('WAHA history pagination repeated a page at offset=%s; stopping safely', offset)
                break
            previous_signature = signature

            for raw in rows:
                raw_id = str(raw.get('id') or '')
                if raw_id and raw_id in seen_raw_ids:
                    continue
                if raw_id:
                    seen_raw_ids.add(raw_id)

                stats['fetched'] = int(stats['fetched']) + 1
                try:
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
                        stats['skipped_self'] = int(stats['skipped_self']) + 1
                        continue

                    if message.has_media and message.media:
                        try:
                            context = await self.media.context_for_memory(message.media)
                            if context:
                                message.body = (
                                    message.body + '\n\n--- Media context ---\n' + context
                                ).strip()
                                stats['media_enriched'] = int(stats['media_enriched']) + 1
                        except MediaProcessingError as exc:
                            logger.warning('History media extraction failed for %s: %s', message.message_id, exc)

                    if not message.body.strip():
                        stats['empty'] = int(stats['empty']) + 1
                        continue

                    status = await self.runtime.ingest_detailed(message.knowledge_payload())
                    if status == 'inserted':
                        stats['inserted'] = int(stats['inserted']) + 1
                    elif status == 'repaired':
                        stats['repaired'] = int(stats['repaired']) + 1
                    else:
                        stats['duplicates'] = int(stats['duplicates']) + 1
                except Exception:
                    stats['failed'] = int(stats['failed']) + 1
                    logger.exception('History row failed at offset=%s id=%s', offset, raw_id)

                if int(stats['fetched']) >= max_messages:
                    break

            # WAHA docs explicitly recommend increasing offset by the requested
            # limit even when a filtered page returns fewer rows.
            offset += limit

        stats['indexed_or_repaired'] = int(stats['inserted']) + int(stats['repaired'])
        return stats
