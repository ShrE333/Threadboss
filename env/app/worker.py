from __future__ import annotations

import asyncio
import logging
import os
import socket

from .config import get_settings
from .processor import EventProcessor
from .redis_bus import EventBus


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    logger = logging.getLogger('threadboss.worker')

    # Give each replica a unique Redis Streams consumer name unless explicitly configured.
    if settings.redis_consumer_name == 'worker-1':
        settings.redis_consumer_name = f'{socket.gethostname()}-{os.getpid()}'

    bus = EventBus(settings)
    processor = EventProcessor(settings, bus)
    await bus.ensure_group()

    logger.info('ThreadBoss worker started: stream=%s group=%s consumer=%s',
                settings.redis_stream, settings.redis_consumer_group, settings.redis_consumer_name)

    async for redis_id, message in bus.consume():
        last_error: Exception | None = None
        for attempt in range(1, settings.worker_max_attempts + 1):
            try:
                await processor.process(message)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                # V1.4 claimed the event before processing. That made retries get
                # discarded as duplicates after a transient WAHA/DNS failure.
                # Release the claim so the next attempt actually runs again.
                await bus.release_event_claim(message.event_id)
                logger.exception(
                    'Processing attempt %s/%s failed for Redis event %s',
                    attempt, settings.worker_max_attempts, redis_id,
                )
                if attempt < settings.worker_max_attempts:
                    await asyncio.sleep(settings.worker_retry_base_seconds * (2 ** (attempt - 1)))

        if last_error is not None:
            # Never let a permanently failing event clog the consumer group forever.
            # Preserve it in a dead-letter stream for later inspection/replay.
            await bus.dead_letter(redis_id, message, repr(last_error))
            logger.error('Moved event to dead letter stream: %s', redis_id)

        await bus.ack(redis_id)


if __name__ == '__main__':
    asyncio.run(run())
