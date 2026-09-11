from __future__ import annotations

from typing import AsyncIterator

import json

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .config import Settings
from .models import MediaRef, NormalizedMessage


class EventBus:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                self.settings.redis_stream,
                self.settings.redis_consumer_group,
                id='0',
                mkstream=True,
            )
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    async def publish(self, message: NormalizedMessage) -> str:
        return await self.redis.xadd(
            self.settings.redis_stream,
            {'message': message.model_dump_json()},
            maxlen=100000,
            approximate=True,
        )

    async def consume(self) -> AsyncIterator[tuple[str, NormalizedMessage]]:
        await self.ensure_group()
        while True:
            rows = await self.redis.xreadgroup(
                groupname=self.settings.redis_consumer_group,
                consumername=self.settings.redis_consumer_name,
                streams={self.settings.redis_stream: '>'},
                count=self.settings.worker_batch_size,
                block=self.settings.worker_block_ms,
            )
            if not rows:
                continue
            for _, messages in rows:
                for redis_id, fields in messages:
                    raw = fields.get('message')
                    if not raw:
                        await self.ack(redis_id)
                        continue
                    yield redis_id, NormalizedMessage.model_validate_json(raw)

    async def dead_letter(self, redis_id: str, message: NormalizedMessage, error: str) -> None:
        await self.redis.xadd(
            self.settings.redis_deadletter_stream,
            {
                'original_redis_id': redis_id,
                'message': message.model_dump_json(),
                'error': error[:2000],
            },
            maxlen=10000,
            approximate=True,
        )

    async def ack(self, redis_id: str) -> None:
        await self.redis.xack(
            self.settings.redis_stream,
            self.settings.redis_consumer_group,
            redis_id,
        )

    async def claim_event(self, event_id: str) -> bool:
        key = f'threadboss:dedupe:{event_id}'
        result = await self.redis.set(
            key,
            '1',
            nx=True,
            ex=self.settings.event_dedupe_ttl_seconds,
        )
        return bool(result)

    async def get_owner(self, session: str) -> str | None:
        return await self.redis.get(f'threadboss:owner:{session}')

    async def set_owner(self, session: str, owner_id: str) -> None:
        await self.redis.set(
            f'threadboss:owner:{session}',
            owner_id,
            ex=self.settings.owner_cache_ttl_seconds,
        )

    async def get_owner_lid(self, session: str) -> str | None:
        return await self.redis.get(f'threadboss:owner_lid:{session}')

    async def set_owner_lid(self, session: str, owner_lid: str) -> None:
        await self.redis.set(
            f'threadboss:owner_lid:{session}',
            owner_lid,
            ex=self.settings.owner_cache_ttl_seconds,
        )


    async def set_menu_state(self, session: str, chat_id: str, state: str) -> None:
        await self.redis.set(
            f'threadboss:menu_state:{session}:{chat_id}',
            state,
            ex=self.settings.menu_state_ttl_seconds,
        )

    async def get_menu_state(self, session: str, chat_id: str) -> str | None:
        return await self.redis.get(f'threadboss:menu_state:{session}:{chat_id}')

    async def clear_menu_state(self, session: str, chat_id: str) -> None:
        await self.redis.delete(f'threadboss:menu_state:{session}:{chat_id}')

    async def remember_menu_poll(
        self, session: str, chat_id: str, poll_id: str, state: str
    ) -> None:
        key = f'threadboss:menu_poll:{session}:{poll_id}'
        await self.redis.set(
            key,
            json.dumps({'chat_id': chat_id, 'state': state}),
            ex=self.settings.menu_state_ttl_seconds,
        )

    async def get_menu_poll(self, session: str, poll_id: str) -> dict | None:
        raw = await self.redis.get(f'threadboss:menu_poll:{session}:{poll_id}')
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def remember_attachment(self, session: str, chat_id: str, media: MediaRef) -> None:
        key = f'threadboss:attachments:{session}:{chat_id}'
        await self.redis.rpush(key, media.model_dump_json())
        await self.redis.ltrim(key, -self.settings.attachment_memory_max_items, -1)
        await self.redis.expire(key, self.settings.attachment_memory_seconds)

    async def recent_attachments(self, session: str, chat_id: str) -> list[MediaRef]:
        key = f'threadboss:attachments:{session}:{chat_id}'
        rows = await self.redis.lrange(key, 0, -1)
        refs: list[MediaRef] = []
        for raw in rows:
            try:
                refs.append(MediaRef.model_validate(json.loads(raw)))
            except Exception:
                continue
        return refs

    async def clear_attachments(self, session: str, chat_id: str) -> None:
        await self.redis.delete(f'threadboss:attachments:{session}:{chat_id}')

    async def close(self) -> None:
        await self.redis.aclose()
